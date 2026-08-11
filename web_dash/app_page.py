from __future__ import annotations

import csv
import hashlib
from functools import lru_cache
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeAlias, TypedDict

from apps._config import (
    APP_FRIENDLY_NAME_MAX_LENGTH,
    CLIENT_PACK_CHANGELOG_MAX_LENGTH,
    CLIENT_PACK_FILENAME_PLACEHOLDERS,
    CLIENT_PACK_FILENAME_TEMPLATE_MAX_LENGTH,
    CLIENT_PACK_METADATA_DESCRIPTION_MAX_LENGTH,
    CLIENT_PACK_METADATA_NAME_MAX_LENGTH,
    ModDistributionMode,
    ModDownloadBlockReason,
    app_title_font_default_label,
    app_title_font_options,
    launcher_provider_label,
    mod_capabilities_for_scope,
    normalise_app_title_font,
    resolve_app_title_font,
)
from font_assets import font_assets
from mod_web_theme import mod_web_tooltip_css

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
from .app_page_factorio import ModWebAppPageFactorioMixin
from .app_page_sevendays import ModWebAppPageSevenDaysMixin
from .app_page_updates import ModWebAppPageUpdateMixin
from .assets import extract_html_tag_contents
from .constants import (
    _APP_ACTION_NOTIFICATION_TIMEOUT_MILLISECONDS,
    _APP_MOD_SORT_QUERY_PARAM,
    _APP_RUNTIME_REFRESH_INTERVAL_SECONDS,
    _APP_SEARCH_QUERY_PARAM,
    _APP_SECTION_QUERY_PARAM,
    _DIRECT_UPLOAD_TOKEN_REFRESH_SECONDS,
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
    AppVersionSource,
    Awaitable,
    BadgeTone,
    BulkLauncherMetadataStatus,
    Button,
    Callable,
    Card,
    Checkbox,
    ClientPackConfig,
    ClientPackFilePreview,
    ClientPackKubeJsScript,
    ClientPackMetadataConfig,
    ClientPackPolicy,
    Html,
    Input,
    Label,
    LiteralString,
    Mapping,
    ModPlacement,
    ModType,
    ModWebUser,
    NodeApiScope,
    NodeAppActivityProviderEntry,
    NodeAppMutationAction,
    NodeAppRuntimeSummary,
    NodeAppStateStreamEvent,
    NodeAppTransitionState,
    NodeConsoleActionList,
    NodeModDependencyEntry,
    NodeModDependencyResolutionResult,
    NodeModEntry,
    NodeModMutationAction,
    NodeModPortalVersionEntry,
    NodeModPortalVersionList,
    NodeModSummary,
    NodeSaveList,
    NodeSettingList,
    NodeSystemSummary,
    PackFormat,
    PackPurpose,
    Power_Level,
    Select,
    Table,
    Textarea,
    Timer,
    Upload,
    assert_never,
    asyncio,
    cast,
    config,
    dataclass,
    escape,
    json,
    parse_qsl,
    quote,
    replace,
    required_app_mutation_level,
    required_mod_mutation_level,
    urlencode,
    urlsplit,
    uuid,
)
from .service_base import ModWebServiceSupport
from .types import (
    ModDownloadKind,
    ModWebAppSectionKind,
    ModWebAppTabDefinition,
    ModWebAppTabLoadResult,
    ModWebBasePageModel,
    ModWebDirectUploadTarget,
    ModWebModlistFormat,
    ModWebModSortOrder,
    ModWebOverviewPageModel,
    ModWebPageLoadWarning,
    ModWebPageModel,
    _ModWebAppHeroCornerBindings,
    _ModWebAppHeroRuntimeDetails,
    _ModWebBadgeSpec,
    _ModWebChatSurfaceConfig,
    _ModWebEnableableControl,
    _ModWebKillControlState,
    _ModWebModToolbarBindings,
    _ModWebNodePresenceBadgeSpec,
    _ModWebRuntimeToolbarBindings,
    _ModWebStartStopControlState,
    _ModWebTabActionSpec,
)
from .ui_helpers import ModWebUiHelpersMixin, copy_text_to_clipboard
from .utils import _format_player_capacity

if TYPE_CHECKING:
    from nicegui.element import Element
    from nicegui.elements.dialog import Dialog
    from nicegui.elements.tabs import Tab
    from nicegui.elements.tooltip import Tooltip
    from nicegui.events import TableSelectionEventArguments


_LEAFLET_VENDOR_DIRECTORY: Path = Path(__file__).resolve().parent.parent / "resources" / "vendor" / "leaflet"
_VIRTUALIZED_LIST_MIN_ITEMS = 50


@dataclass(frozen=True, slots=True)
class _ModWebSectionChromeBindings:
    endpoint_count_label: Label | None = None
    endpoint_count_tooltip: "Tooltip | None" = None
    endpoint_count_tooltip_content: Html | None = None
    mod_update_check_badge: Label | None = None


class _VirtualModRow(TypedDict):
    name: str
    friendly: str
    file: str
    size: str
    update_available: bool
    placement: str
    policy: str
    type: str
    type_tone: str
    downloadable: bool
    download_block_label: str
    selectable: bool
    show_download_block: bool
    show_placement: bool
    show_policy: bool
    state_class: str


class _BulkMetadataRow(TypedDict):
    name: str
    friendly: str
    status: str
    providers: str
    suggested_type: str
    suggested_type_selectable: bool
    apply_suggested_type: bool


@dataclass(frozen=True, slots=True)
class _ModWebDependencySelectionSummary:
    dependency_count: int
    selected_count: int
    installed_count: int


_VirtualModAction: TypeAlias = Literal["details", "download"]

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
    ModWebAppPageFactorioMixin,
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
        load_tab: Callable[[str], Awaitable[ModWebAppTabLoadResult]] | None = None,
    ) -> None:
        if chat_surface is not None and not model.supports_chat:
            raise ValueError("App page received chat configuration for an app without chat support.")
        self._apply_theme_for_user(ui=ui, user=user)
        ModWebUiHelpersMixin._render_skip_link(ui=ui)
        current_model: ModWebBasePageModel = model
        last_system_summary: NodeSystemSummary | None = initial_system_summary
        with ui.column().classes(self._app_page_classes()).props("id=mod-main-content role=main tabindex=-1"):
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
            tabs: tuple[ModWebAppTabDefinition, ...] = self._page_tabs_for_user(model=model, user=user)
            apply_section_runtime_model: Callable[[ModWebBasePageModel], None] | None = (
                self._render_tabbed_page_sections(
                    ui=ui,
                    model=model,
                    user=user,
                    current_url=current_url,
                    tabs=tabs,
                    chat_surface=chat_surface,
                    refresh_async_runtime_model=refresh_async_runtime_model,
                    load_tab=load_tab,
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
        load_tab: Callable[[str], Awaitable[ModWebAppTabLoadResult]] | None = None,
    ) -> None:
        if chat_surface is not None and not model.supports_chat:
            raise ValueError("Overview page received chat configuration for an app without chat support.")
        self._apply_theme_for_user(ui=ui, user=user)
        ModWebUiHelpersMixin._render_skip_link(ui=ui)
        current_model: ModWebBasePageModel = model
        last_system_summary: NodeSystemSummary | None = initial_system_summary
        with ui.column().classes(self._app_page_classes()).props("id=mod-main-content role=main tabindex=-1"):
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
            tabs: tuple[ModWebAppTabDefinition, ...] = self._page_tabs_for_user(model=model, user=user)
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
                        load_tab=load_tab,
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
            relay_badge = _ModWebBadgeSpec(text="Unknown", tone="grey", tooltip_text="Chat bridge support")
            version_badge = _ModWebBadgeSpec(
                text="Unknown",
                tone="black",
                tooltip_text=self._app_version_badge_tooltip(AppVersionSource.STARTUP),
            )
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

        relay_badge = _ModWebBadgeSpec(
            text=f"{app_stats.relay_support.display_value}",
            tone="grey",
            tooltip_text="Chat bridge support",
        )
        version_badge = _ModWebBadgeSpec(
            text=f"{app_stats.version or 'Unknown'}",
            tone="black",
            tooltip_text=self._app_version_badge_tooltip(app_stats.version_source),
        )
        player_count_badge: _ModWebBadgeSpec | None = None
        if app_stats.player_count is not None and app_stats.player_capacity is not None:
            player_tone = "purple" if app_stats.player_count > 0 else "grey"
            player_capacity_text: str | None = _format_player_capacity(app_stats.player_capacity)
            if player_capacity_text is None:
                raise RuntimeError("Player capacity unexpectedly missing for runtime details badge.")
            player_count_badge = _ModWebBadgeSpec(
                text=f"{app_stats.player_count} / {player_capacity_text}",
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
    def _app_version_badge_tooltip(version_source: AppVersionSource) -> str:
        if version_source is AppVersionSource.INSTALLED_FILES:
            return "Game version updated live"
        return "Game version updated upon start"

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
        detail_value: str | None = provider.detail_value
        provider_id: str = provider.provider_id.casefold()
        if detail_value is not None:
            lines.extend(tuple(line for line in detail_value.splitlines() if line.strip()))
        elif provider_id == "day" and current_value.startswith("D") and current_value[1:].isdigit():
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
        if provider_id.casefold() == "map_age":
            return escape(current_value)
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
            return f"{resource_name} points required for running"
        return f"{resource_name} points required for running (starting)"

    @staticmethod
    def _app_resource_point_badge_text(*, running_points: int, startup_points: int) -> str:
        if startup_points == running_points:
            return str(running_points)
        return f"{running_points} ({startup_points})"

    @staticmethod
    def _app_page_hero_mod_badge(summary: NodeModSummary) -> _ModWebBadgeSpec:
        total_count: int = summary.total_count
        server_loadable_count: int = summary.server_loadable_count
        enabled_count: int = summary.server_enabled_count
        if total_count == 1 and enabled_count == total_count:
            text = "1 Mod"
        elif summary.client_only_count > 0:
            text = f"{server_loadable_count} server · {summary.client_only_count} client"
        elif enabled_count != server_loadable_count:
            text = f"{enabled_count}/{server_loadable_count} Mods"
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

    def _page_tabs_for_user(
        self,
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
    ) -> tuple[ModWebAppTabDefinition, ...]:
        tabs: tuple[ModWebAppTabDefinition, ...] = self._page_tabs(model)
        if not self._can_open_app_properties(user=user):
            return tabs
        if any(tab.tab_id == "properties" for tab in tabs):
            raise ValueError("App properties tab conflicts with an existing tab.")
        return (*tabs, self._app_properties_tab_definition())

    def _can_open_app_properties(self, *, user: ModWebUser) -> bool:
        return self._user_has_level(
            user, required_app_mutation_level(NodeAppMutationAction.ENABLE)
        ) or self._user_has_level(
            user, required_app_mutation_level(NodeAppMutationAction.UPDATE_DETAILS)
        )

    @staticmethod
    def _app_properties_tab_definition() -> ModWebAppTabDefinition:
        return ModWebAppTabDefinition.custom(
            tab_id="properties",
            label="Properties",
            page_order=700,
            app_card_order=700,
            app_card_tone="grey",
            icon="tune",
            show_on_app_card=False,
            render_handler_name="_render_app_properties_section",
        )

    @staticmethod
    def _is_local_app_properties_tab(tab: ModWebAppTabDefinition) -> bool:
        return tab.render_handler_name == "_render_app_properties_section"

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
                    tooltip_text="Node owning this app",
                )
                if color_hex := self._node_role_color_hex(node_name=node_name):
                    node_badge.style(self._node_badge_style(color_hex))
                relay_badge = self._badge(
                    ui=ui,
                    text=initial_runtime_details.relay_badge.text,
                    tone=initial_runtime_details.relay_badge.tone,
                    extra_classes="mod-app-corner-badge",
                    tooltip_text=initial_runtime_details.relay_badge.tooltip_text,
                )
                version_badge = self._badge(
                    ui=ui,
                    text=initial_runtime_details.version_badge.text,
                    tone=initial_runtime_details.version_badge.tone,
                    extra_classes="mod-app-corner-badge",
                    tooltip_text=initial_runtime_details.version_badge.tooltip_text,
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
        node = self._remote_node_link(node_name)
        return node.presence_stream_url if node.is_current else None

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
                activity_badge_bindings: list[tuple[Element, Html]] = []
                for index, _provider in enumerate(activity_providers):
                    with ui.element("span").classes(self._badge_class_name(tone="black")) as activity_badge_wrapper:
                        activity_badge_content = cast(
                            Html,
                            ui.html(initial_activity_badges[index] if index < len(initial_activity_badges) else ""),
                        )
                    activity_badge_bindings.append((activity_badge_wrapper, activity_badge_content))
                activity_badges: tuple[Element, ...] = tuple(binding[0] for binding in activity_badge_bindings)
                activity_badge_contents: tuple[Html, ...] = tuple(binding[1] for binding in activity_badge_bindings)
                activity_badge_tooltips = tuple(
                    self._attach_html_tooltip(
                        ui=ui,
                        target=activity_badge,
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
                    for index, activity_badge in enumerate(activity_badges)
                )

        current_runtime_details: _ModWebAppHeroRuntimeDetails = initial_runtime_details

        def _apply_runtime(app_stats: NodeAppRuntimeSummary | None) -> None:
            nonlocal current_runtime_details
            hero_card.classes(replace=self._app_hero_card_classes(app_stats))
            runtime_details = self._app_hero_runtime_details(app_stats)
            status_changed: bool = (
                runtime_details.status_text != current_runtime_details.status_text
                or runtime_details.status_tone != current_runtime_details.status_tone
            )
            if runtime_details.status_text != current_runtime_details.status_text:
                status_value_label.set_text(runtime_details.status_text)
            status_value_label.classes(
                replace=f"mod-app-hero-status-value mod-app-hero-status-value-{runtime_details.status_tone}"
            )
            if status_changed:
                self._pulse_live_value(status_value_label)
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
            for index, (activity_badge, activity_badge_content, activity_badge_markup, activity_provider) in enumerate(
                zip(
                    activity_badges,
                    activity_badge_contents,
                    visible_activity_badges,
                    visible_activity_providers,
                    strict=False,
                )
            ):
                activity_badge_content.set_content(activity_badge_markup)
                activity_badge_content.update()
                activity_tooltip, activity_tooltip_content = activity_badge_tooltips[index]
                self._set_html_tooltip_state(
                    activity_tooltip,
                    activity_tooltip_content,
                    self._app_activity_provider_tooltip_html(
                        provider=activity_provider,
                        connected_player_names=app_stats.connected_player_names if app_stats is not None else (),
                    ),
                )
                self._set_element_visibility(activity_badge, visible=True)
            for index, activity_badge in enumerate(
                activity_badges[len(visible_activity_badges) :],
                start=len(visible_activity_badges),
            ):
                activity_tooltip, activity_tooltip_content = activity_badge_tooltips[index]
                self._set_html_tooltip_state(activity_tooltip, activity_tooltip_content, "")
                self._set_element_visibility(activity_badge, visible=False)
            self._set_element_visibility(
                runtime_badge_row,
                visible=runtime_details.player_count_badge is not None or bool(visible_activity_badges),
            )
            current_runtime_details = runtime_details

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
                return self._mods_header_badges(
                    model.mods.summary,
                    client_pack_version=self._supported_client_pack_version(model),
                )
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
        can_write: bool = (
            any(self._user_has_level(user, entry.write_power_level) for entry in model.configs.configs)
            if model.configs.configs
            else self._user_has_level(user, model.config_write_level)
        )
        if not can_read:
            return (_ModWebBadgeSpec(text="Locked", tone="grey"),)
        return (
            _ModWebBadgeSpec(text=f"{len(model.configs.configs)} files", tone="grey"),
            _ModWebBadgeSpec(
                text="Write" if can_write else "Read only",
                tone="red" if can_write else "grey",
            ),
        )

    @staticmethod
    def _mod_dependency_entries_by_id(
        resolution: NodeModDependencyResolutionResult,
    ) -> dict[str, NodeModDependencyEntry]:
        return {entry.mod_id: entry for entry in resolution.dependencies}

    @staticmethod
    def _mod_dependency_selected_ids(
        *,
        resolution: NodeModDependencyResolutionResult,
        dependency_checkboxes: Mapping[str, Checkbox],
    ) -> tuple[str, ...]:
        selected_ids: list[str] = [resolution.root_mod_id]
        for entry in resolution.dependencies:
            if entry.is_root:
                continue
            checkbox = dependency_checkboxes.get(entry.mod_id)
            if checkbox is not None and bool(_value_as_object(checkbox)):
                selected_ids.append(entry.mod_id)
        return tuple(selected_ids)

    @staticmethod
    def _mod_dependency_selection_summary(
        *,
        resolution: NodeModDependencyResolutionResult,
        dependency_checkboxes: Mapping[str, Checkbox],
    ) -> _ModWebDependencySelectionSummary:
        dependency_entries = tuple(entry for entry in resolution.dependencies if not entry.is_root)
        selected_count = sum(
            1
            for entry in dependency_entries
            if (
                bool(_value_as_object(checkbox))
                if (checkbox := dependency_checkboxes.get(entry.mod_id)) is not None
                else entry.selected_by_default and not entry.installed
            )
        )
        installed_count = sum(1 for entry in dependency_entries if entry.installed)
        return _ModWebDependencySelectionSummary(
            dependency_count=len(dependency_entries),
            selected_count=selected_count,
            installed_count=installed_count,
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
                f'<input id="{color_id}" class="mod-map-color" type="color" value="#22C55E" aria-label="Annotation colour">'
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
            <style>
              __MOD_WEB_TOOLTIP_CSS__
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
                border: 1px solid var(--mod-accent-border);
                border-radius: 0;
                background: rgba(0, 0, 0, 0.96);
                box-shadow:
                  0 18px 42px rgba(3, 7, 18, 0.34),
                  inset 0 1px 0 rgba(255, 255, 255, 0.05),
                  inset 0 -1px 0 var(--mod-accent-faint);
                transform: translate(-50%, calc(-100% - 0.85rem));
              }
              .mod-map-label-prompt-title {
                color: var(--mod-accent-text-strong);
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
                border: 1px solid var(--mod-accent-border);
                border-radius: 0;
                background:
                  linear-gradient(180deg, var(--mod-accent-wash), transparent),
                  var(--mod-accent-surface);
                box-shadow:
                  inset 0 1px 0 rgba(255, 255, 255, 0.04),
                  inset 0 -1px 0 var(--mod-accent-glow);
                color: var(--mod-accent-text-strong);
              }
              .mod-map-label-input::placeholder {
                color: var(--mod-accent-border-strong);
              }
              .mod-map-label-input:focus {
                outline: none;
                border-color: var(--mod-accent-border-strong);
                box-shadow:
                  0 0 0 1px var(--mod-accent-border),
                  inset 0 1px 0 rgba(255, 255, 255, 0.04),
                  inset 0 -1px 0 var(--mod-accent-glow);
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
                border: 1px solid var(--mod-accent-glow);
                background:
                  linear-gradient(180deg, var(--mod-accent-faint), rgba(24, 24, 27, 0)),
                  rgba(12, 10, 18, 0.62);
                box-shadow:
                  inset 0 1px 0 rgba(255, 255, 255, 0.03),
                  inset 0 -1px 0 var(--mod-accent-wash);
              }
              .mod-map-select,
              .mod-map-color,
              .mod-map-toggle {
                border: 1px solid var(--mod-accent-border);
                border-radius: 0;
                background:
                  linear-gradient(180deg, var(--mod-accent-wash), transparent),
                  var(--mod-accent-surface);
                box-shadow:
                  inset 0 1px 0 rgba(255, 255, 255, 0.04),
                  inset 0 -1px 0 var(--mod-accent-glow);
                color: var(--mod-accent-text-strong);
                min-height: 2.5rem;
              }
              .mod-map-select {
                flex: 1 1 12rem;
                min-width: 10rem;
                padding: 0 0.75rem;
                appearance: none;
              }
              .mod-map-select option {
                background: var(--mod-accent-surface);
                color: var(--mod-accent-text-strong);
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
                border-color: var(--mod-accent-border-strong) !important;
                box-shadow:
                  inset 0 1px 0 rgba(255, 255, 255, 0.06),
                  inset 0 -1px 0 var(--mod-accent-glow),
                  0 0 0 1px var(--mod-accent-faint) !important;
                background:
                  linear-gradient(180deg, var(--mod-accent-glow), var(--mod-accent-faint)),
                  var(--mod-accent-surface) !important;
                color: var(--mod-accent-text-strong) !important;
              }
              .mod-map-toggle {
                display: inline-flex;
                align-items: center;
                gap: 0.4rem;
                font-size: 0.92rem;
                min-width: 2.5rem;
                padding: 0 0.75rem;
                color: var(--mod-accent-text-strong);
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
                accent-color: var(--mod-accent);
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
                border: 1px solid var(--mod-accent-faint);
                overflow: hidden;
                background:
                  radial-gradient(circle at top, rgba(56, 189, 248, 0.22), transparent 34%),
                  linear-gradient(180deg, rgba(226, 232, 240, 0.94), rgba(248, 250, 252, 0.98));
                box-shadow:
                  0 20px 48px rgba(3, 7, 18, 0.26),
                  0 0 0 1px var(--mod-accent-wash);
              }
              .mod-map-canvas .leaflet-container {
                width: 100%;
                height: 100%;
                background: transparent;
                font: inherit;
              }
              .mod-map-canvas .leaflet-control-zoom {
                border: 1px solid var(--mod-accent-glow);
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
                  linear-gradient(180deg, var(--mod-accent-text-strong), var(--mod-accent-text-strong));
                color: var(--mod-accent-dark);
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
            "__MOD_WEB_TOOLTIP_CSS__", mod_web_tooltip_css()
        ).replace("__LEAFLET_JS__", _leaflet_vendor_asset("leaflet.js"))

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
    def _initial_page_search_query(current_url: str) -> str:
        query_by_key: dict[str, str] = {
            key: value for key, value in parse_qsl(urlsplit(current_url).query, keep_blank_values=True)
        }
        return query_by_key.get(_APP_SEARCH_QUERY_PARAM, "").strip()

    @staticmethod
    def _initial_page_mod_sort_order(current_url: str) -> ModWebModSortOrder:
        query_by_key: dict[str, str] = {
            key: value for key, value in parse_qsl(urlsplit(current_url).query, keep_blank_values=True)
        }
        raw_order: str = query_by_key.get(_APP_MOD_SORT_QUERY_PARAM, "").strip()
        try:
            return ModWebModSortOrder(raw_order)
        except ValueError:
            return ModWebModSortOrder.NEWEST

    @staticmethod
    def _replace_browser_query_value(*, ui: ModWebUi, param_name: str, value: str) -> None:
        encoded_value: str = json.dumps(value.strip())
        encoded_param: str = json.dumps(param_name)
        ui.run_javascript(
            f"""
            (() => {{
              const url = new URL(window.location.href);
              const value = {encoded_value};
              if (value) {{
                url.searchParams.set({encoded_param}, value);
              }} else {{
                url.searchParams.delete({encoded_param});
              }}
              window.history.replaceState(window.history.state, '', `${{url.pathname}}${{url.search}}${{url.hash}}`);
            }})();
            """
        )

    @classmethod
    def _replace_browser_search_query(cls, *, ui: ModWebUi, search_query: str) -> None:
        cls._replace_browser_query_value(
            ui=ui,
            param_name=_APP_SEARCH_QUERY_PARAM,
            value=search_query,
        )

    @classmethod
    def _replace_browser_mod_sort_order(cls, *, ui: ModWebUi, order: ModWebModSortOrder) -> None:
        cls._replace_browser_query_value(
            ui=ui,
            param_name=_APP_MOD_SORT_QUERY_PARAM,
            value="" if order is ModWebModSortOrder.NEWEST else order.value,
        )

    @staticmethod
    def _page_tab_url(current_url: str, *, tab_id: str) -> str:
        query_by_key: dict[str, str] = {
            key: value for key, value in parse_qsl(urlsplit(current_url).query, keep_blank_values=True)
        }
        updated_url: str = ModWebUiHelpersMixin._request_url_with_query_values(
            current_url,
            param_name=_APP_SECTION_QUERY_PARAM,
            values=(tab_id,),
        )
        current_tab_id: str = query_by_key.get(_APP_SECTION_QUERY_PARAM, "").strip().casefold()
        if current_tab_id and current_tab_id != tab_id.casefold():
            return ModWebUiHelpersMixin._request_url_with_query_values(
                updated_url,
                param_name=_APP_SEARCH_QUERY_PARAM,
                values=(),
            )
        return updated_url

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
        load_tab: Callable[[str], Awaitable[ModWebAppTabLoadResult]] | None = None,
    ) -> Callable[[ModWebBasePageModel], None] | None:
        if not tabs:
            return None

        has_local_app_properties_tab: bool = any(self._is_local_app_properties_tab(tab) for tab in tabs)
        initial_tab_id: str = self._initial_page_tab_id(current_url=current_url, tabs=tabs)
        current_section_url: str = self._page_tab_url(current_url, tab_id=initial_tab_id)
        section_model: ModWebBasePageModel = replace(
            model,
            search_query=self._initial_page_search_query(current_url),
            mod_sort_order=self._initial_page_mod_sort_order(current_url),
        )
        tab_by_id: dict[str, Tab] = {}
        section_chrome_by_tab_id: dict[str, "Element"] = {}
        section_panel_by_tab_id: dict[str, "Element"] = {}
        section_runtime_appliers: list[Callable[[ModWebBasePageModel], None]] = []
        loaded_tab_ids: set[str] = {initial_tab_id}
        loading_tab_ids: set[str] = set()

        def set_section_chrome_visibility(tab_id: str) -> None:
            for chrome_tab_id, chrome in section_chrome_by_tab_id.items():
                if chrome_tab_id == tab_id:
                    chrome.style(remove="display: none;")
                else:
                    chrome.style(add="display: none;")

        def render_section_chrome(
            *,
            tab: ModWebAppTabDefinition,
            tab_model: ModWebBasePageModel,
            tab_chat_surface: _ModWebChatSurfaceConfig | None,
        ) -> _ModWebSectionChromeBindings:
            section_actions: tuple[_ModWebTabActionSpec, ...] = self._page_tab_actions(
                model=tab_model,
                user=user,
                tab=tab,
                chat_surface=tab_chat_surface,
            )
            if tab.builtin_kind is ModWebAppSectionKind.CHAT:
                if tab_chat_surface is None:
                    raise ValueError("The Chat tab requires a chat surface configuration.")
                with ui.row().classes("mod-section-chrome-badge-row items-center justify-start gap-2 flex-wrap"):
                    if tab_chat_surface.map_url is not None:
                        self._badge_link(
                            ui=ui,
                            text="Map",
                            tone="purple",
                            url=tab_chat_surface.map_url,
                            new_tab=True,
                        )
                    endpoint_label, endpoint_tooltip, endpoint_tooltip_content = self._render_chat_endpoint_badge(
                        ui=ui,
                        snapshot=tab_chat_surface.panel.initial_snapshot,
                    )
                show_fake_chat = (
                    tab_chat_surface.publish_fake_event is not None
                    and self._user_has_level(user, Power_Level.root)
                )
                if section_actions or show_fake_chat:
                    with ui.row().classes("mod-section-chrome-actions items-center justify-start gap-2 flex-wrap"):
                        for action in section_actions:
                            self._action_link(
                                ui=ui,
                                label=action.label,
                                url=action.url,
                                compact=True,
                                extra_classes=action.extra_classes,
                                new_tab=action.new_tab,
                            )
                        if show_fake_chat:
                            assert tab_chat_surface.publish_fake_event is not None
                            self._render_fake_chat_preview_control(
                                ui=ui,
                                user=user,
                                app_name=tab_chat_surface.panel.initial_snapshot.room_id,
                                app_friendly=tab_chat_surface.app_friendly,
                                publish_event=tab_chat_surface.publish_fake_event,
                            )
                return _ModWebSectionChromeBindings(
                    endpoint_count_label=endpoint_label,
                    endpoint_count_tooltip=endpoint_tooltip,
                    endpoint_count_tooltip_content=endpoint_tooltip_content,
                )

            section_badges: tuple[_ModWebBadgeSpec, ...] = self._page_section_badges(
                model=tab_model,
                user=user,
                tab=tab,
            )
            if section_badges:
                with ui.row().classes("mod-section-chrome-badge-row items-center justify-start gap-2 flex-wrap"):
                    for badge in section_badges:
                        self._badge(ui=ui, text=badge.text, tone=badge.tone)
                    mod_update_check_badge: Label | None = None
                    if tab.builtin_kind is ModWebAppSectionKind.MODS:
                        mod_update_check_badge = self._badge(ui=ui, text="Checking", tone="grey")
                        self._set_element_visibility(mod_update_check_badge, visible=False)
            else:
                mod_update_check_badge = None
            if section_actions:
                with ui.row().classes("mod-section-chrome-actions items-center justify-start gap-2 flex-wrap"):
                    for action in section_actions:
                        self._action_link(
                            ui=ui,
                            label=action.label,
                            url=action.url,
                            compact=True,
                            extra_classes=action.extra_classes,
                            new_tab=action.new_tab,
                        )
            return _ModWebSectionChromeBindings(mod_update_check_badge=mod_update_check_badge)

        def render_section(
            *,
            tab: ModWebAppTabDefinition,
            tab_model: ModWebBasePageModel,
            tab_chat_surface: _ModWebChatSurfaceConfig | None,
        ) -> None:
            chrome = section_chrome_by_tab_id[tab.tab_id]
            chrome.clear()
            with chrome:
                chrome_bindings = render_section_chrome(
                    tab=tab,
                    tab_model=tab_model,
                    tab_chat_surface=tab_chat_surface,
                )

            panel = section_panel_by_tab_id[tab.tab_id]
            panel.clear()
            with panel:
                section_runtime_model = self._render_page_section(
                    ui=ui,
                    model=tab_model,
                    user=user,
                    tab=tab,
                    chat_surface=tab_chat_surface,
                    refresh_async_runtime_model=refresh_async_runtime_model,
                    chat_endpoint_count_label=chrome_bindings.endpoint_count_label,
                    chat_endpoint_tooltip=chrome_bindings.endpoint_count_tooltip,
                    chat_endpoint_tooltip_content=chrome_bindings.endpoint_count_tooltip_content,
                    mod_update_check_badge=chrome_bindings.mod_update_check_badge,
                )
            if section_runtime_model is not None:
                section_runtime_appliers.append(section_runtime_model)

        async def sync_section(event: ModWebValueContainer) -> None:
            nonlocal current_section_url
            next_tab_id: str = _value_as_text(event).strip()
            if next_tab_id not in tab_by_id:
                return
            current_section_url = self._page_tab_url(current_section_url, tab_id=next_tab_id)
            self._replace_browser_url(ui=ui, target_url=current_section_url)
            set_section_chrome_visibility(next_tab_id)
            if next_tab_id in loaded_tab_ids or next_tab_id in loading_tab_ids:
                return
            next_tab = next(tab for tab in tabs if tab.tab_id == next_tab_id)
            if self._is_local_app_properties_tab(next_tab):
                render_section(
                    tab=next_tab,
                    tab_model=section_model,
                    tab_chat_surface=None,
                )
                loaded_tab_ids.add(next_tab_id)
                return
            if load_tab is None:
                ui.navigate.to(current_section_url)
                return

            loading_tab_ids.add(next_tab_id)
            target_panel = section_panel_by_tab_id[next_tab_id]
            target_panel.clear()
            with target_panel:
                ui.label("Loading tab…").classes("mod-subtitle text-sm mod-tab-loading").props(
                    "role=status aria-live=polite"
                )
            try:
                loaded = await load_tab(next_tab_id)
                if not self._ui_client_is_alive(ui=ui):
                    return
                render_section(
                    tab=next_tab,
                    tab_model=loaded.model,
                    tab_chat_surface=loaded.chat_surface,
                )
                loaded_tab_ids.add(next_tab_id)
            except Exception as xcp:
                log.warning(
                    "App tab load failed: node=%s app=%s tab=%s error=%s",
                    model.node_name,
                    model.app_name,
                    next_tab_id,
                    xcp,
                )
                if not self._ui_client_is_alive(ui=ui):
                    return
                target_panel.clear()
                with target_panel:
                    self._render_flat_tab_empty_state(
                        ui=ui,
                        title="Tab unavailable",
                        description=f"Could not load this tab: {xcp}",
                    )
            finally:
                loading_tab_ids.discard(next_tab_id)

        with ui.column().classes("mod-section-layout w-full"):
            with ui.row().classes("mod-section-strip w-full items-start gap-3 flex-wrap"):
                with ui.element("div").classes("mod-section-tabs-shell"):
                    with (
                        ui.tabs(value=initial_tab_id, on_change=sync_section)
                        .classes("mod-section-tabs")
                        .props("aria-label=App sections")
                    ) as section_tabs:
                        for tab in tabs:
                            tab_element = ui.tab(tab.tab_id, label=tab.label, icon=tab.icon)
                            tab_by_id[tab.tab_id] = tab_element
                            self._attach_badge_tooltip(
                                ui=ui,
                                target=tab_element,
                                text=f"Open {tab.label} section",
                            )
            with ui.row().classes("mod-section-chrome w-full items-start justify-start gap-3 flex-wrap"):
                for tab in tabs:
                    chrome_classes = "mod-section-chrome-panel items-start justify-start gap-3 flex-wrap"
                    if tab.builtin_kind is ModWebAppSectionKind.CHAT:
                        chrome_classes += " mod-section-chrome-chat"
                    section_chrome_by_tab_id[tab.tab_id] = ui.row().classes(chrome_classes)
            with ui.tab_panels(
                section_tabs,
                value=initial_tab_id,
                animated=False,
            ).classes("mod-section-panels w-full"):
                for tab in tabs:
                    section_panel_by_tab_id[tab.tab_id] = (
                        ui.tab_panel(tab_by_id[tab.tab_id]).classes("mod-section-panel w-full")
                    )
            initial_tab = next(tab for tab in tabs if tab.tab_id == initial_tab_id)
            render_section(
                tab=initial_tab,
                tab_model=section_model,
                tab_chat_surface=chat_surface,
            )
            set_section_chrome_visibility(initial_tab_id)
        if current_url != current_section_url:
            self._replace_browser_url(ui=ui, target_url=current_section_url)
        if not section_runtime_appliers and not has_local_app_properties_tab:
            return None

        def apply_section_runtime_model(runtime_model: ModWebBasePageModel) -> None:
            nonlocal section_model
            section_model = runtime_model
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
        mod_update_check_badge: Label | None = None,
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
                self._render_mods_section(
                    ui=ui,
                    model=model,
                    user=user,
                    mod_update_check_badge=mod_update_check_badge,
                )
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
            if entry.client_pack_eligible and entry.client_pack.policy is ClientPackPolicy.OPTIONAL
        }
        unknown_optional_names: frozenset[str] = optional_names.difference(optional_entries)
        if unknown_optional_names:
            raise ValueError(f"Unknown optional client-pack mods: {', '.join(sorted(unknown_optional_names))}")

        choice_groups: dict[str, frozenset[str]] = {}
        for entry in mods:
            if not entry.client_pack_eligible or entry.client_pack.policy is not ClientPackPolicy.ALTERNATIVE:
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
            if entry.client_pack_eligible
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

    @staticmethod
    def _client_pack_format_options(app_scope: str | None) -> dict[str, str]:
        options: dict[str, str] = {}
        if mod_capabilities_for_scope(app_scope).supports_launcher_formats:
            options.update(
                {
                    PackFormat.MODRINTH.value: "Modrinth (.mrpack)",
                    PackFormat.CURSEFORGE.value: "CurseForge ZIP",
                }
            )
        options[PackFormat.GENERIC_ZIP.value] = "Generic ZIP"
        return options

    @staticmethod
    def _default_client_pack_format(app_scope: str | None) -> PackFormat:
        if mod_capabilities_for_scope(app_scope).supports_launcher_formats:
            return PackFormat.MODRINTH
        return PackFormat.GENERIC_ZIP

    @staticmethod
    def _supported_client_pack_version(model: ModWebPageModel) -> str | None:
        if not mod_capabilities_for_scope(model.app_scope).supports_client_pack:
            return None
        return model.client_pack_published_version

    @staticmethod
    def _supports_client_pack(model: ModWebPageModel) -> bool:
        return mod_capabilities_for_scope(model.app_scope).supports_client_pack

    @staticmethod
    def _render_modlist(
        mods: tuple[NodeModEntry, ...],
        *,
        instance_name: str,
        pack_version: str | None,
        output_format: ModWebModlistFormat,
        include_version: bool,
        include_filename: bool,
        include_pack_version: bool = True,
        include_disabled: bool = False,
        include_builtin: bool = False,
        include_client: bool = True,
    ) -> str:
        ordered_mods = tuple(
            sorted(
                (
                    entry
                    for entry in mods
                    if (include_disabled or entry.placement is not ModPlacement.SERVER_DISABLED)
                    and (include_builtin or entry.mod_type is not ModType.BUILTIN)
                    and (include_client or entry.mod_type is not ModType.CLIENT)
                ),
                key=lambda entry: (entry.friendly.casefold(), entry.name.casefold()),
            )
        )

        def is_optional(entry: NodeModEntry) -> bool:
            return entry.client_pack.policy is ClientPackPolicy.OPTIONAL

        def structured_item(entry: NodeModEntry) -> dict[str, object]:
            item: dict[str, object] = {"name": entry.friendly}
            if include_version:
                item["version"] = entry.version
            if include_filename:
                item["filename"] = entry.archive_name
            if is_optional(entry):
                item["optional"] = True
            return item

        def display_line(entry: NodeModEntry, escape_text: Callable[[str], str]) -> str:
            parts: list[str] = [escape_text(entry.friendly)]
            if is_optional(entry):
                parts.append("[Optional]")
            if include_version:
                parts.append(f"[{escape_text(entry.version or 'Unknown')}]")
            if include_filename:
                parts.append(f"({escape_text(entry.archive_name)})")
            return " ".join(parts)

        def markdown_text(value: str) -> str:
            escaped = value.replace("\\", "\\\\").replace("\n", " ")
            for character in "`*_[]<>":
                escaped = escaped.replace(character, f"\\{character}")
            return escaped

        def discord_text(value: str) -> str:
            escaped = value.replace("\\", "\\\\").replace("\n", " ")
            for character in "*_~|":
                escaped = escaped.replace(character, f"\\{character}")
            return escaped

        def discord_code(value: str) -> str:
            delimiter = "`"
            while delimiter in value:
                delimiter += "`"
            return f"{delimiter}{value}{delimiter}"

        version_label = pack_version or "Unpublished"
        plaintext_heading = f"{instance_name} [{version_label}]" if include_pack_version else instance_name

        match output_format:
            case ModWebModlistFormat.PLAINTEXT:
                body = "\n".join(display_line(entry, lambda value: value) for entry in ordered_mods)
                return f"{plaintext_heading}\n\n{body}" if body else plaintext_heading
            case ModWebModlistFormat.JSON:
                payload = [structured_item(entry) for entry in ordered_mods]
                return json.dumps(payload, ensure_ascii=False, indent=4)
            case ModWebModlistFormat.JSONL:
                return "\n".join(json.dumps(structured_item(entry), ensure_ascii=False) for entry in ordered_mods)
            case ModWebModlistFormat.CSV:
                columns = ["name"]
                if include_version:
                    columns.append("version")
                if include_filename:
                    columns.append("filename")
                if any(is_optional(entry) for entry in ordered_mods):
                    columns.append("optional")
                output = StringIO(newline="")
                writer = csv.writer(output, lineterminator="\n")
                writer.writerow(columns)
                for entry in ordered_mods:
                    item = structured_item(entry)
                    row: list[object] = []
                    for column in columns:
                        value = item.get(column)
                        row.append(value if value is not None else "")
                    writer.writerow(row)
                return output.getvalue().rstrip("\n")
            case ModWebModlistFormat.MARKDOWN_GFM:

                def markdown_cell(value: str) -> str:
                    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")

                columns = ["Name"]
                if include_version:
                    columns.append("Version")
                if include_filename:
                    columns.append("Filename")
                header = f"| {' | '.join(columns)} |"
                separator = f"| {' | '.join('---' for _column in columns)} |"
                rows: list[str] = [header, separator]
                for entry in ordered_mods:
                    display_name = markdown_cell(entry.friendly)
                    if is_optional(entry):
                        display_name = f"{display_name} [Optional]"
                    cells = [display_name]
                    if include_version:
                        cells.append(markdown_cell(entry.version or "Unknown"))
                    if include_filename:
                        cells.append(markdown_cell(entry.archive_name))
                    rows.append(f"| {' | '.join(cells)} |")
                heading = (
                    f"# {markdown_text(instance_name)} [{markdown_text(version_label)}]"
                    if include_pack_version
                    else f"# {markdown_text(instance_name)}"
                )
                body = "\n".join(rows)
                return f"{heading}\n\n{body}"
            case ModWebModlistFormat.MARKDOWN_COMMONMARK:
                body = "\n".join(f"- {display_line(entry, markdown_text)}" for entry in ordered_mods)
                heading = (
                    f"# {markdown_text(instance_name)} [{markdown_text(version_label)}]"
                    if include_pack_version
                    else f"# {markdown_text(instance_name)}"
                )
                return f"{heading}\n\n{body}" if body else heading
            case ModWebModlistFormat.DISCORD:
                lines: list[str] = []
                for entry in ordered_mods:
                    parts = [f"**{discord_text(entry.friendly)}**"]
                    if is_optional(entry):
                        parts.append("[Optional]")
                    if include_version:
                        parts.append(f"[{discord_code(entry.version or 'Unknown')}]")
                    if include_filename:
                        parts.append(f"({discord_code(entry.archive_name)})")
                    lines.append(f"- {' '.join(parts)}")
                heading = (
                    f"**{discord_text(instance_name)} [{discord_text(version_label)}]**"
                    if include_pack_version
                    else f"**{discord_text(instance_name)}**"
                )
                body = "\n".join(lines)
                return f"{heading}\n\n{body}" if body else heading
            case _:
                assert_never(output_format)

    @staticmethod
    def _show_client_pack_kubejs_toggle(
        app_scope: str | None,
        scripts: tuple[ClientPackKubeJsScript, ...],
    ) -> bool:
        return (
            app_scope is not None and app_scope.casefold() == "minecraft" and any(script.included for script in scripts)
        )

    @staticmethod
    def _default_mod_download_names(
        mods: tuple[NodeModEntry, ...],
        distribution_mode: ModDistributionMode,
    ) -> frozenset[str]:
        return frozenset(
            entry.name
            for entry in mods
            if entry.downloadable
            and entry.mod_type is not ModType.BUILTIN
            and (distribution_mode is not ModDistributionMode.RAW_ENABLED or entry.enabled)
        )

    def _render_mods_section(
        self,
        *,
        ui: ModWebUi,
        model: ModWebPageModel,
        user: ModWebUser,
        mod_update_check_badge: Label | None = None,
    ) -> None:
        capabilities = mod_capabilities_for_scope(model.app_scope)
        is_minecraft_app: bool = (
            model.app_scope is not None and model.app_scope.casefold() == "minecraft"
        )
        show_client_pack_kubejs_toggle: bool = self._show_client_pack_kubejs_toggle(
            model.app_scope,
            model.client_pack_kubejs_scripts,
        )
        client_pack_metadata: ClientPackMetadataConfig = model.client_pack_metadata or ClientPackMetadataConfig(
            name=model.app_friendly,
        )
        checkboxes: dict[str, Checkbox] = {}
        virtual_mod_table: Table | None = None
        virtual_mod_rows: list[_VirtualModRow] = []
        mod_options = self._mod_options(model.mods.mods)
        show_search: bool = len(mod_options) > 1
        show_sort: bool = len(mod_options) > 1
        current_search_query: str = model.search_query
        current_sort_order: ModWebModSortOrder = model.mod_sort_order
        downloadable_names: tuple[str, ...] = tuple[str, ...](
            entry.name for entry in model.mods.mods if entry.downloadable
        )
        selected_mod_names: set[str] = set(
            self._default_mod_download_names(model.mods.mods, capabilities.mode)
        )
        optional_client_entries: tuple[NodeModEntry, ...] = tuple(
            entry
            for entry in model.mods.mods
            if entry.client_pack_eligible and entry.client_pack.policy is ClientPackPolicy.OPTIONAL
        )
        client_choice_groups: dict[str, tuple[NodeModEntry, ...]] = {}
        for entry in model.mods.mods:
            client_pack = entry.client_pack
            if not entry.client_pack_eligible or client_pack.policy is not ClientPackPolicy.ALTERNATIVE:
                continue
            if client_pack.choice_group is None:
                raise ValueError(f"Alternative client-pack mod {entry.name!r} has no choice group.")
            client_choice_groups[client_pack.choice_group] = (
                *client_choice_groups.get(client_pack.choice_group, ()),
                entry,
            )
        supports_client_pack: bool = capabilities.supports_client_pack
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
        can_install_factorio_mod_link: bool = can_upload_mod and model.app_scope == "factorio"
        selection_button = None
        download_button = None
        delete_control: _ModWebEnableableControl | None = None
        result_count_label: Label | None = None
        delete_selected_button: Button | None = None
        metadata_status_button: Button | None = None
        metadata_ui_container: Card | None = None
        metadata_operation_task: asyncio.Task[None] | None = None
        metadata_operation_id: str | None = None
        metadata_cancel_requested = False
        metadata_active_status = ""
        available_update_mod_names: set[str] = (
            set(self._cached_mod_update_names(model=model, entries=model.mods.mods))
            if model.app_scope == config.AppScopes.factorio.value and self._user_has_level(user, Power_Level.user)
            else set()
        )
        checking_all_mod_updates = False

        def set_metadata_status(text: str, *, running: bool) -> None:
            nonlocal metadata_active_status
            metadata_active_status = text
            if metadata_status_button is not None:
                metadata_status_button.set_text(f"Metadata: {text}")
                metadata_status_button.set_visibility(True)
                metadata_status_button.set_enabled(running)

        def start_metadata_operation(
            action: Callable[[str], Awaitable[None]],
        ) -> None:
            nonlocal metadata_operation_task, metadata_operation_id, metadata_cancel_requested
            if metadata_operation_task is not None and not metadata_operation_task.done():
                ui.notify("A metadata operation is already running.", type="warning")
                return
            operation_id = str(uuid.uuid4())
            metadata_operation_id = operation_id
            metadata_cancel_requested = False
            container = metadata_ui_container
            if container is None:
                raise RuntimeError("Metadata UI container was not rendered.")

            async def run() -> None:
                nonlocal metadata_operation_task, metadata_operation_id, metadata_cancel_requested
                with container:
                    try:
                        await action(operation_id)
                    except asyncio.CancelledError:
                        log.info(
                            "Bulk mod metadata operation cancelled: node=%s app=%s operation=%s",
                            model.node_name,
                            model.app_name,
                            operation_id,
                        )
                        set_metadata_status("Cancelled", running=False)
                        ui.notify("Metadata operation cancelled.", type="warning")
                    except Exception as xcp:
                        log.exception(
                            "Bulk mod metadata background task failed: node=%s app=%s "
                            "operation=%s",
                            model.node_name,
                            model.app_name,
                            operation_id,
                        )
                        set_metadata_status("Failed", running=False)
                        ui.notify(f"Metadata operation failed: {xcp}", type="negative")
                    finally:
                        if metadata_operation_id == operation_id:
                            metadata_operation_task = None
                            metadata_operation_id = None
                            metadata_cancel_requested = False

            metadata_operation_task = asyncio.create_task(run())

        def cancel_metadata_operation() -> None:
            nonlocal metadata_cancel_requested
            operation_task = metadata_operation_task
            operation_id = metadata_operation_id
            if operation_task is None or operation_task.done() or operation_id is None:
                return
            if metadata_cancel_requested:
                return
            metadata_cancel_requested = True
            previous_status = metadata_active_status
            set_metadata_status("Cancelling…", running=False)
            log.info(
                "Bulk mod metadata cancellation requested from dashboard: node=%s app=%s operation=%s",
                model.node_name,
                model.app_name,
                operation_id,
            )

            async def cancel() -> None:
                nonlocal metadata_cancel_requested
                container = metadata_ui_container
                if container is None:
                    raise RuntimeError("Metadata UI container was not rendered.")
                with container:
                    try:
                        cancelled = False
                        for attempt in range(3):
                            cancelled = await self._cancel_bulk_mod_metadata(
                                model=model,
                                operation_id=operation_id,
                                user=user,
                            )
                            if cancelled or operation_task.done():
                                break
                            if attempt < 2:
                                await asyncio.sleep(0.25)
                    except Exception as xcp:
                        log.warning(
                            "Bulk mod metadata cancellation failed: node=%s app=%s operation=%s error=%s",
                            model.node_name,
                            model.app_name,
                            operation_id,
                            xcp,
                        )
                        metadata_cancel_requested = False
                        if not operation_task.done():
                            set_metadata_status(previous_status, running=True)
                        ui.notify(f"Metadata cancellation failed: {xcp}", type="negative")
                        return
                    if cancelled:
                        operation_task.cancel()
                        return
                    metadata_cancel_requested = False
                    if not operation_task.done():
                        set_metadata_status(previous_status, running=True)
                        ui.notify("Metadata operation could not be cancelled yet.", type="warning")

            asyncio.create_task(cancel())

        def update_result_count(visible_count: int) -> None:
            if result_count_label is None:
                return
            result_count_label.set_text(
                self._mod_result_count_label(
                    visible_count=visible_count,
                    total_count=len(model.mods.mods),
                )
            )

        def set_current_update_check(entry: NodeModEntry | None) -> None:
            if mod_update_check_badge is None:
                return
            if entry is None:
                self._set_element_visibility(mod_update_check_badge, visible=False)
                return
            mod_update_check_badge.set_text(f"Checking {entry.friendly}")
            self._set_element_visibility(mod_update_check_badge, visible=True)

        async def check_all_mod_updates() -> None:
            nonlocal checking_all_mod_updates
            if checking_all_mod_updates:
                ui.notify("Mod update checks are already running.", type="warning")
                return
            checking_all_mod_updates = True
            total_mod_count = len(model.mods.mods)
            ui.notify(
                f"Checking {total_mod_count} mod{'s' if total_mod_count != 1 else ''} for updates…",
                type="info",
            )
            try:
                batch_result = await self._check_all_mod_updates(
                    model=model,
                    entries=model.mods.mods,
                    user=user,
                    on_checking=set_current_update_check,
                )
            except Exception as xcp:
                log.warning(
                    "Bulk mod update check failed: node=%s app=%s error=%s",
                    model.node_name,
                    model.app_name,
                    xcp,
                )
                ui.notify(f"Mod update checks failed: {xcp}", type="negative")
                return
            finally:
                checking_all_mod_updates = False
                set_current_update_check(None)
            available_update_mod_names.clear()
            available_update_mod_names.update(batch_result.update_mod_names)
            _mod_download_rows.refresh(current_search_query)
            available_update_count = len(batch_result.update_mod_names)
            failure_count = len(batch_result.failed_mod_names)
            message = (
                f"Checked {batch_result.checked_mod_count} mod"
                f"{'s' if batch_result.checked_mod_count != 1 else ''}: "
                f"{available_update_count} update{'s' if available_update_count != 1 else ''} available."
            )
            if failure_count:
                message = (
                    f"{message} {failure_count} mod"
                    f"{'s' if failure_count != 1 else ''} could not be checked."
                )
            ui.notify(message, type="warning" if failure_count else "positive")

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
            if selection_button is None:
                return
            selected_count: int = len(selected_mod_names)
            selected_downloadable_count: int = len(selected_downloadable_mod_names_in_page_order())
            selected_deletable_count: int = len(selected_deletable_mod_names_in_page_order())
            selection_button.set_text(self._selection_toggle_label(selected_count=selected_count))
            if download_button is not None:
                download_button.set_text(
                    self._download_selection_label(
                        selected_count=selected_downloadable_count,
                        downloadable_count=downloadable_count,
                    )
                )
                can_download: bool = downloadable_count > 0 and (
                    not selected_mod_names or selected_downloadable_count > 0
                )
                download_button.set_enabled(can_download)
            selection_button.set_enabled(bool(selectable_names))
            if delete_control is not None:
                delete_control.set_enabled(selected_deletable_count > 0)

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
            if virtual_mod_table is not None:
                selected_rows = [
                    row for row in virtual_mod_rows if row["name"] in selected_mod_names
                ]
                virtual_mod_table.selected = cast(
                    list[dict[object, object]],
                    cast(object, selected_rows),
                )
                virtual_mod_table.update()
            update_count()

        def clear_selection() -> None:
            selected_mod_names.clear()
            for checkbox in checkboxes.values():
                checkbox.set_value(False)
            if virtual_mod_table is not None:
                virtual_mod_table.selected = []
                virtual_mod_table.update()
            update_count()

        def toggle_selection() -> None:
            if selected_mod_names:
                clear_selection()
            else:
                select_all()

        async def download_selected() -> None:
            mod_names: tuple[str, ...] = selected_downloadable_mod_names_in_page_order()
            if mod_names:
                excluded_names = tuple(
                    mod_name for mod_name in downloadable_names if mod_name not in selected_mod_names
                )
                selected_query_length = len(urlencode({"mod_name": mod_names}, doseq=True))
                excluded_query_length = len(urlencode({"mod_name": excluded_names}, doseq=True))
                use_excluded_names = excluded_query_length < selected_query_length
                query: str = urlencode(
                    self._download_query(
                        enabled_only=False,
                        selected_only=True,
                        excluded_only=use_excluded_names,
                        mod_names=excluded_names if use_excluded_names else mod_names,
                    ),
                    doseq=True,
                )
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
            await self._start_download(
                ui=ui,
                user=user,
                model=model,
                url=(
                    model.download_enabled_url
                    if capabilities.mode is ModDistributionMode.RAW_ENABLED
                    else model.download_all_url
                ),
                message=self._download_feedback_message(
                    kind=ModDownloadKind.ALL,
                    app_friendly=model.app_friendly,
                ),
                filenames=(f"{model.app_name}-mods.zip",),
            )

        async def _delete_selected() -> None:
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
            self._guarded_reload(ui=ui)

        async def delete_selected() -> None:
            if delete_selected_button is None:
                raise RuntimeError("Delete selected mods button was not rendered.")
            await self._run_with_loading_button(
                button=delete_selected_button,
                action=_delete_selected,
            )

        inline_upload_control: Upload | None = None
        direct_upload_transfer_id: int | None = None
        upload_placement = ModPlacement.SERVER_ENABLED
        mod_link_input: Input | None = None
        mod_link_version_input: Input | None = None
        mod_link_version_select: Select | None = None
        mod_link_versions_button: Button | None = None
        mod_link_versions_status_label: Label | None = None
        mod_link_versions: NodeModPortalVersionList | None = None
        mod_link_install_button: Button | None = None
        mod_link_resolution: NodeModDependencyResolutionResult | None = None
        mod_link_dependency_checkboxes: dict[str, Checkbox] = {}

        def ensure_direct_upload_transfer() -> int | None:
            nonlocal direct_upload_transfer_id
            if direct_upload_transfer_id is not None:
                return direct_upload_transfer_id
            try:
                direct_upload_transfer_id = self._start_direct_upload_transfer(
                    model=model,
                    user=user,
                    label="Mod upload",
                    detail_text=f"Sending mods directly to {model.app_friendly}.",
                )
            except RuntimeError as xcp:
                ui.notify(f"Upload started, but tray tracking is unavailable: {xcp}", type="warning")
            return direct_upload_transfer_id

        def finish_direct_upload_transfer(*, error: str | None) -> None:
            nonlocal direct_upload_transfer_id
            transfer_id: int | None = direct_upload_transfer_id
            direct_upload_transfer_id = None
            if transfer_id is None:
                return
            if error is None:
                self._backend.complete_transfer(
                    transfer_id=transfer_id,
                    detail_text=f"Installed mods for {model.app_friendly}.",
                )
            else:
                self._backend.fail_transfer(transfer_id=transfer_id, detail_text=error)

        def interrupt_direct_upload_transfer() -> None:
            if direct_upload_transfer_id is None:
                return
            finish_direct_upload_transfer(error="Mod upload was interrupted because the app page was closed.")

        def mod_portal_version_option_label(version: NodeModPortalVersionEntry) -> str:
            return version.version

        def open_upload_picker() -> None:
            if inline_upload_control is None:
                raise RuntimeError("Inline mod upload control is not available.")
            upload_placement_dialog.open()

        def choose_upload_placement(placement: ModPlacement) -> None:
            nonlocal upload_placement
            if inline_upload_control is None:
                raise RuntimeError("Inline mod upload control is not available.")
            upload_placement = placement
            upload_placement_dialog.close()
            refresh_direct_upload_target()
            inline_upload_control.run_method("pickFiles")

        def refresh_direct_upload_target() -> None:
            if inline_upload_control is None:
                raise RuntimeError("Inline mod upload control is not available.")
            target: ModWebDirectUploadTarget = self._direct_mod_upload_target(
                model=model,
                user=user,
                placement=upload_placement,
            )
            inline_upload_control.props["url"] = target.url
            inline_upload_control.props["headers"] = [
                {"name": "Authorization", "value": target.authorization_header},
            ]

        def direct_upload_started() -> None:
            ui.notify(
                f"Upload acknowledged. Sending mods directly to {model.app_friendly}.",
                type="info",
            )
            ensure_direct_upload_transfer()

        def direct_upload_succeeded() -> None:
            finish_direct_upload_transfer(error=None)
            ui.notify(f"Uploaded mods for {model.app_friendly}.", type="positive")
            self._guarded_reload(ui=ui)

        def direct_upload_failed() -> None:
            error = f"Mod upload failed before {model.app_friendly} accepted it."
            ensure_direct_upload_transfer()
            finish_direct_upload_transfer(error=error)
            ui.notify(
                f"{error} "
                "The node may be unavailable, out of temporary space, or may have rejected the files.",
                type="negative",
                multi_line=True,
            )

        def direct_upload_rejected() -> None:
            error = "The selected mod files were rejected before upload."
            ensure_direct_upload_transfer()
            finish_direct_upload_transfer(error=error)
            ui.notify(
                f"{error} Check file and batch limits.",
                type="warning",
            )

        def open_mod_link_dialog() -> None:
            nonlocal mod_link_resolution, mod_link_versions
            mod_link_resolution = None
            mod_link_versions = None
            mod_link_dependency_checkboxes.clear()
            if mod_link_input is not None:
                mod_link_input.set_value("")
            if mod_link_version_input is not None:
                mod_link_version_input.set_value("")
            if mod_link_versions_status_label is not None:
                mod_link_versions_status_label.set_text("")
            mod_link_version_control.refresh()
            mod_link_dialog_body.refresh()
            mod_link_dialog.open()

        def selected_mod_link_version() -> str | None:
            if mod_link_version_select is not None:
                selected_version = _value_as_text(mod_link_version_select).strip()
                return selected_version or None
            if mod_link_version_input is None:
                raise RuntimeError("Mod link version input was not rendered.")
            version = _value_as_text(mod_link_version_input).strip()
            return version or None

        async def _load_mod_link_versions() -> None:
            nonlocal mod_link_versions, mod_link_resolution
            if mod_link_input is None:
                raise RuntimeError("Mod link input was not rendered.")
            if mod_link_versions_status_label is None:
                raise RuntimeError("Mod link versions status label was not rendered.")
            try:
                loaded_versions = await self._mod_link_versions(
                    model=model,
                    url_to_install=_value_as_text(mod_link_input),
                    user=user,
                )
            except Exception as xcp:
                mod_link_versions = None
                mod_link_versions_status_label.set_text("Version lookup failed.")
                ui.notify(f"Mod version lookup failed: {xcp}", type="negative", multi_line=True)
                mod_link_version_control.refresh()
                return
            mod_link_versions = loaded_versions
            mod_link_resolution = None
            mod_link_dependency_checkboxes.clear()
            mod_link_versions_status_label.set_text(
                f"{len(loaded_versions.versions)} compatible version"
                f"{'s' if len(loaded_versions.versions) != 1 else ''}."
            )
            mod_link_version_control.refresh()
            mod_link_dialog_body.refresh()

        async def load_mod_link_versions() -> None:
            if mod_link_versions_button is None:
                raise RuntimeError("Mod link versions button was not rendered.")
            await self._run_with_loading_button(
                button=mod_link_versions_button,
                action=_load_mod_link_versions,
            )

        async def _install_mod_link_from_dialog() -> None:
            if mod_link_input is None:
                raise RuntimeError("Mod link input was not rendered.")
            raw_url = _value_as_text(mod_link_input)
            if mod_link_resolution is None:
                raise RuntimeError("Mod link dependencies were not resolved.")
            try:
                result = await self._install_mod_link(
                    model=model,
                    url_to_install=raw_url,
                    user=user,
                    selected_mod_ids=self._mod_dependency_selected_ids(
                        resolution=mod_link_resolution,
                        dependency_checkboxes=mod_link_dependency_checkboxes,
                    ),
                    version=selected_mod_link_version(),
                )
            except Exception as xcp:
                ui.notify(f"Mod link install failed: {xcp}", type="negative", multi_line=True)
                return
            mod_link_dialog.close()
            ui.notify(result.message, type="positive")
            self._guarded_reload(ui=ui)

        async def _resolve_mod_link_from_dialog() -> None:
            nonlocal mod_link_resolution
            if mod_link_input is None:
                raise RuntimeError("Mod link input was not rendered.")
            raw_url = _value_as_text(mod_link_input)
            try:
                mod_link_resolution = await self._resolve_mod_link(
                    model=model,
                    url_to_install=raw_url,
                    user=user,
                    version=selected_mod_link_version(),
                )
            except Exception as xcp:
                ui.notify(f"Mod link resolve failed: {xcp}", type="negative", multi_line=True)
                return
            mod_link_dependency_checkboxes.clear()
            mod_link_dialog_body.refresh()

        async def install_mod_link_from_dialog() -> None:
            if mod_link_install_button is None:
                raise RuntimeError("Mod link install button was not rendered.")
            await self._run_with_loading_button(
                button=mod_link_install_button,
                action=(
                    _resolve_mod_link_from_dialog if mod_link_resolution is None else _install_mod_link_from_dialog
                ),
            )

        optional_client_checkboxes: dict[str, Checkbox] = {}
        client_choice_selects: dict[str, Select] = {}
        client_pack_format_select: Select | None = None
        include_kubejs_scripts_checkbox: Checkbox | None = None
        include_servers_dat_checkbox: Checkbox | None = None
        include_options_txt_checkbox: Checkbox | None = None
        client_pack_file_previews: dict[str, ClientPackFilePreview] = {
            preview.path: preview for preview in model.client_pack_file_previews
        }
        client_pack_file_preview_title: Label | None = None
        client_pack_file_preview_content: Textarea | None = None
        client_pack_file_preview_dialog: Dialog | None = None

        def ensure_client_pack_file_preview_dialog() -> None:
            nonlocal client_pack_file_preview_content
            nonlocal client_pack_file_preview_dialog
            nonlocal client_pack_file_preview_title
            if client_pack_file_preview_dialog is not None:
                return
            client_pack_file_preview_dialog = ui.dialog()
            with client_pack_file_preview_dialog:
                with ui.card().classes("mod-card mod-dialog-card"):
                    with ui.column().classes("w-full gap-4 p-5"):
                        client_pack_file_preview_title = ui.label("View client file").classes(
                            "text-xl font-black mod-title-small"
                        )
                        client_pack_file_preview_content = (
                            ui.textarea(
                                "Content",
                                value="",
                            )
                            .props("filled square readonly hide-bottom-space color=accent rows=12")
                            .classes("w-full mod-config-input")
                        )
                        with ui.row().classes("w-full justify-end gap-2"):
                            ui.button("Close", on_click=client_pack_file_preview_dialog.close).classes(
                                "mod-list-button secondary"
                            )

        def open_client_pack_file_preview(preview: ClientPackFilePreview) -> None:
            ensure_client_pack_file_preview_dialog()
            if client_pack_file_preview_title is None or client_pack_file_preview_content is None:
                raise RuntimeError("Client pack file preview dialog was not rendered.")
            client_pack_file_preview_title.set_text(f"View {preview.display_name}")
            client_pack_file_preview_content.set_value(preview.content_text)
            if client_pack_file_preview_dialog is None:
                raise RuntimeError("Client pack file preview dialog was not rendered.")
            client_pack_file_preview_dialog.open()

        def _normalised_client_pack_node_label(node_name: str) -> str:
            text = node_name.strip()
            if not text:
                return "Node"
            if text.casefold() == text:
                return text.title()
            return text

        def _fallback_servers_dat_preview() -> ClientPackFilePreview:
            server_address = model.join_direct_ip_address or model.join_address
            if server_address is None:
                content_text = "No join address is configured, so overrides/servers.dat will not be generated."
            else:
                node_label = _normalised_client_pack_node_label(model.node_name)
                server_base = "".join(character for character in node_label if character.isalnum()) or "Node"
                content_text = (
                    "Minecraft servers.dat entry\n"
                    f"name={server_base}Server\n"
                    f"ip={server_address}\n"
                )
            return ClientPackFilePreview(
                path="overrides/servers.dat",
                display_name="servers.dat",
                content_text=content_text,
            )

        def client_pack_file_preview(path: str, display_name: str) -> ClientPackFilePreview:
            preview = client_pack_file_previews.get(path)
            if preview is not None:
                return preview
            if path == "overrides/servers.dat":
                return _fallback_servers_dat_preview()
            return ClientPackFilePreview(
                path=path,
                display_name=display_name,
                content_text=f"No preview is available for {display_name}.",
            )

        def render_client_pack_file_option(
            *,
            label: str,
            value: bool,
            preview_path: str,
            preview_display_name: str,
        ) -> Checkbox:
            preview = client_pack_file_preview(preview_path, preview_display_name)
            with ui.row().classes("mod-client-pack-file-option w-full items-center gap-2"):
                checkbox = (
                    ui.checkbox(label, value=value)
                    .props("dense color=accent keep-color")
                    .classes("mod-client-pack-checkbox grow")
                )
                with ui.button(
                    icon="visibility",
                    on_click=lambda file_preview=preview: open_client_pack_file_preview(file_preview),
                ).props(
                    f"flat round dense color=accent aria-label=View {preview_display_name}"
                ).classes("mod-client-pack-file-view-button"):
                    ui.tooltip(f"View {preview_display_name}")
                return checkbox
        with ui.dialog() as upload_placement_dialog:
            with ui.card().classes("mod-card mod-dialog-card"):
                with ui.column().classes("w-full gap-4 p-5"):
                    ui.label("Upload mod files").classes("text-xl font-black mod-title-small")
                    ui.label("Choose where the uploaded files belong.").classes("mod-subtitle text-sm")
                    with ui.row().classes("w-full gap-2 flex-wrap"):
                        ui.button(
                            "Server enabled",
                            on_click=lambda: choose_upload_placement(ModPlacement.SERVER_ENABLED),
                        ).classes("mod-list-button")
                        if capabilities.supports_client_only:
                            ui.button(
                                "Client only",
                                on_click=lambda: choose_upload_placement(ModPlacement.CLIENT_ONLY),
                            ).classes("mod-list-button secondary")
                    with ui.row().classes("w-full justify-end"):
                        ui.button("Cancel", on_click=upload_placement_dialog.close).classes(
                            "mod-list-button secondary"
                        )

        with ui.dialog() as mod_link_dialog:
            with ui.card().classes("mod-card mod-dialog-card"):
                with ui.column().classes("w-full gap-4 p-5"):
                    ui.label("Add Mod Link").classes("text-xl font-black mod-title-small")

                    def reset_mod_link_resolution(_event: object | None = None) -> None:
                        nonlocal mod_link_resolution
                        mod_link_resolution = None
                        mod_link_dependency_checkboxes.clear()
                        mod_link_dialog_body.refresh()

                    def reset_mod_link_url_state(_event: object | None = None) -> None:
                        nonlocal mod_link_versions
                        mod_link_versions = None
                        if mod_link_versions_status_label is not None:
                            mod_link_versions_status_label.set_text("")
                        reset_mod_link_resolution()
                        mod_link_version_control.refresh()

                    mod_link_input = (
                        ui.input(placeholder="https://mods.factorio.com/mod/example")
                        .props("filled square dense clearable hide-bottom-space color=accent")
                        .classes("w-full mod-config-input")
                    )
                    mod_link_input.on("update:model-value", reset_mod_link_url_state)
                    mod_link_input.on("keydown.enter", install_mod_link_from_dialog)
                    with ui.row().classes("w-full gap-2 items-center"):
                        @ui.refreshable
                        def mod_link_version_control() -> None:
                            nonlocal mod_link_version_input, mod_link_version_select
                            mod_link_version_input = None
                            mod_link_version_select = None
                            if mod_link_versions is None:
                                mod_link_version_input = (
                                    ui.input("Version", placeholder="Latest compatible")
                                    .props(
                                        "filled square dense clearable stack-label hide-bottom-space color=accent"
                                    )
                                    .classes("grow mod-config-input")
                                )
                                mod_link_version_input.on("update:model-value", reset_mod_link_resolution)
                                mod_link_version_input.on("keydown.enter", install_mod_link_from_dialog)
                                return
                            loaded_versions = mod_link_versions
                            version_options = {
                                "": "Latest compatible",
                                **{
                                    version.version: mod_portal_version_option_label(version)
                                    for version in loaded_versions.versions
                                },
                            }
                            mod_link_version_select = (
                                ui.select(version_options, value="", label="Version")
                                .props("filled square dense stack-label hide-bottom-space color=accent options-dark")
                                .classes("grow mod-config-input")
                            )
                            mod_link_version_select.on("update:model-value", reset_mod_link_resolution)

                        mod_link_version_control()
                        mod_link_versions_button = ui.button(
                            "Load Versions",
                            on_click=load_mod_link_versions,
                        ).classes("mod-list-button secondary shrink-0")
                    mod_link_versions_status_label = ui.label("").classes("mod-subtitle text-xs")

                    @ui.refreshable
                    def mod_link_dialog_body() -> None:
                        if mod_link_resolution is None:
                            ui.label(
                                "Paste a mod page URL to inspect the dependency graph before installing."
                            ).classes("mod-subtitle text-sm")
                            if mod_link_install_button is not None:
                                mod_link_install_button.set_text("Check Dependencies")
                            return

                        entries_by_id = self._mod_dependency_entries_by_id(mod_link_resolution)
                        root_entry = entries_by_id.get(mod_link_resolution.root_mod_id)
                        if root_entry is None:
                            raise ValueError("Resolved mod dependency graph is missing the root mod.")
                        summary = self._mod_dependency_selection_summary(
                            resolution=mod_link_resolution,
                            dependency_checkboxes=mod_link_dependency_checkboxes,
                        )

                        def render_dependency_tree(mod_id: str, rendered_mod_ids: set[str]) -> None:
                            entry = entries_by_id.get(mod_id)
                            if entry is None:
                                return
                            repeated = mod_id in rendered_mod_ids and not entry.is_root
                            if not repeated:
                                rendered_mod_ids.add(mod_id)
                            child_ids = tuple(
                                child_id for child_id in entry.dependency_mod_ids if child_id in entries_by_id
                            )
                            with ui.row().classes("w-full items-start gap-3 mod-tab-toolbar-surface"):
                                if entry.is_root:
                                    root_icon = ui.icon("link").classes("text-lg text-white/80 mt-1")
                                    with root_icon:
                                        ui.tooltip("Requested mod from the link. This is always included.")
                                elif repeated:
                                    repeated_icon = ui.icon("share").classes("text-lg text-white/60 mt-1")
                                    with repeated_icon:
                                        ui.tooltip("Shared dependency already shown elsewhere in the tree.")
                                else:
                                    checkbox = ui.checkbox(value=entry.selected_by_default and not entry.installed)
                                    checkbox.props("dense color=accent")
                                    if entry.installed:
                                        checkbox.disable()
                                        with checkbox:
                                            ui.tooltip("Already installed. Left deselected to avoid re-downloading.")
                                    else:
                                        with checkbox:
                                            ui.tooltip("Download this dependency.")
                                    mod_link_dependency_checkboxes[entry.mod_id] = checkbox
                                with ui.column().classes("gap-1 flex-1 min-w-0"):
                                    with ui.row().classes("w-full items-start justify-between gap-3"):
                                        with ui.row().classes("items-center gap-2 min-w-0"):
                                            ui.label(entry.title).classes("mod-setting-name break-all")
                                            if len(entry.parent_mod_ids) > 1:
                                                with ui.row().classes(
                                                    "items-center gap-1 rounded px-2 py-0.5 text-[0.65rem] "
                                                    "uppercase tracking-[0.18em] bg-white/5 text-white/55"
                                                ):
                                                    parent_icon = ui.icon("hub").classes("text-xs")
                                                    with parent_icon:
                                                        ui.tooltip(
                                                            f"Required by {len(entry.parent_mod_ids)} parent mods."
                                                        )
                                                    ui.label(str(len(entry.parent_mod_ids)))
                                        ui.label(entry.version).classes("mod-subtitle text-xs shrink-0 text-white/60")
                                    with ui.row().classes("items-center gap-2 text-white/55 min-w-0 flex-wrap"):
                                        ui.label(entry.mod_id).classes("mod-subtitle text-xs break-all")
                                        if entry.installed:
                                            installed_icon = ui.icon("check_circle").classes("text-sm")
                                            with installed_icon:
                                                ui.tooltip("Already installed on the server.")
                                        elif repeated:
                                            repeated_child_icon = ui.icon("subdirectory_arrow_right").classes("text-sm")
                                            with repeated_child_icon:
                                                ui.tooltip("Shown under another parent above.")
                                        if child_ids and not repeated:
                                            with ui.row().classes(
                                                "items-center gap-1 rounded px-2 py-0.5 text-[0.65rem] "
                                                "uppercase tracking-[0.18em] bg-white/5 text-white/55"
                                            ):
                                                child_icon = ui.icon("account_tree").classes("text-xs")
                                                with child_icon:
                                                    ui.tooltip(
                                                        f"Has {len(child_ids)} direct dependencies."
                                                    )
                                                ui.label(str(len(child_ids)))
                            if repeated or not child_ids:
                                return
                            with ui.column().classes("w-full gap-2 pl-5 ml-3 border-l border-white/10"):
                                for child_id in child_ids:
                                    render_dependency_tree(child_id, rendered_mod_ids)

                        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                            for icon_name, count, tooltip_text in (
                                ("account_tree", summary.dependency_count, "Extra dependency mods in this graph."),
                                ("download", summary.selected_count, "Dependency mods selected for download."),
                                ("download_done", summary.installed_count, "Dependency mods already installed."),
                            ):
                                with ui.row().classes(
                                    "items-center gap-2 rounded-md px-3 py-2 bg-white/5 text-white/75"
                                ):
                                    summary_icon = ui.icon(icon_name).classes("text-base")
                                    with summary_icon:
                                        ui.tooltip(tooltip_text)
                                    ui.label(str(count)).classes("text-sm font-medium")
                        with ui.column().classes("w-full gap-2"):
                            render_dependency_tree(mod_link_resolution.root_mod_id, set())
                        if mod_link_install_button is not None:
                            mod_link_install_button.set_text("Install")

                    mod_link_dialog_body()
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("Cancel", on_click=mod_link_dialog.close).classes("mod-list-button secondary")
                        mod_link_install_button = ui.button(
                            "Check Dependencies",
                            on_click=install_mod_link_from_dialog,
                        ).classes("mod-list-button")

        def selected_client_pack_format() -> PackFormat:
            return (
                self._default_client_pack_format(model.app_scope)
                if client_pack_format_select is None
                else PackFormat(_value_as_text(client_pack_format_select))
            )

        async def start_client_pack_download(
            *,
            mod_names: tuple[str, ...],
        ) -> None:
            if model.client_pack_content_dirty:
                ui.notify(
                    "Publish the client-pack configuration before downloading it.",
                    type="warning",
                )
                return
            pack_format = selected_client_pack_format()
            client_pack_dialog.close()
            query: str = urlencode(
                self._download_query(
                    enabled_only=False,
                    selected_only=True,
                    mod_names=mod_names,
                    pack_purpose=PackPurpose.CLIENT,
                    pack_format=pack_format,
                    publish_client_pack=False,
                    include_kubejs_scripts=(
                        True
                        if include_kubejs_scripts_checkbox is None
                        else bool(_value_as_object(include_kubejs_scripts_checkbox))
                    ),
                    include_servers_dat=(
                        True
                        if include_servers_dat_checkbox is None
                        else bool(_value_as_object(include_servers_dat_checkbox))
                    ),
                    include_options_txt=(
                        True
                        if include_options_txt_checkbox is None
                        else bool(_value_as_object(include_options_txt_checkbox))
                    ),
                ),
                doseq=True,
            )
            await self._start_download(
                ui=ui,
                user=user,
                model=model,
                url=f"{self._download_base_url(model)}?{query}",
                message=self._download_feedback_message(
                    kind=ModDownloadKind.CLIENT_PACK,
                    app_friendly=model.app_friendly,
                ),
                filenames=(f"{model.app_name}-client-pack{pack_format.suffix}",),
            )

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
            explicitly_selected_names = optional_names.union(choice_names.values())
            selected_choice_names = tuple(
                mod_name for mod_name in mod_names if mod_name in explicitly_selected_names
            )
            await start_client_pack_download(mod_names=selected_choice_names)

        configurable_client_entries: tuple[NodeModEntry, ...] = tuple(
            entry
            for entry in model.mods.mods
            if entry.client_pack_eligible and not self._is_builtin_mod(entry)
        )
        config_policy_selects: dict[str, Select] = {}
        config_group_inputs: dict[str, Input] = {}
        config_group_names: dict[str, str] = {
            entry.name: entry.client_pack.choice_group or "" for entry in configurable_client_entries
        }
        config_default_selects: dict[str, Select] = {}
        config_rows: dict[str, Element] = {}
        config_kubejs_script_checkboxes: dict[str, Checkbox] = {}
        client_pack_name_input: Input | None = None
        client_pack_description_input: Textarea | None = None
        client_pack_filename_template_input: Input | None = None
        client_pack_include_servers_dat_checkbox: Checkbox | None = None
        client_pack_include_options_txt_checkbox: Checkbox | None = None
        client_pack_changelog_draft: str = model.client_pack_changelog or ""
        client_pack_changelog_input: Textarea | None = None
        client_pack_config_save_button: Button | None = None
        client_pack_publish_button: Button | None = None
        config_default_names: dict[str, str] = {
            entry.client_pack.choice_group: entry.name
            for entry in configurable_client_entries
            if entry.client_pack.policy is ClientPackPolicy.ALTERNATIVE
            and entry.client_pack.choice_group is not None
            and entry.client_pack.default_choice
        }

        def configured_choice_groups() -> dict[str, tuple[NodeModEntry, ...]]:
            groups: dict[str, tuple[NodeModEntry, ...]] = {}
            for entry in configurable_client_entries:
                policy_select = config_policy_selects[entry.name]
                if ClientPackPolicy(_value_as_text(policy_select)) is not ClientPackPolicy.ALTERNATIVE:
                    continue
                group_name = config_group_names[entry.name]
                if not group_name or any(character.isspace() for character in group_name):
                    continue
                groups[group_name] = (*groups.get(group_name, ()), entry)
            return groups

        def filter_client_pack_config_rows(event: ModWebEventArgumentsContainer) -> None:
            query_tokens: tuple[str, ...] = tuple(
                token for token in _event_args_as_text(event).casefold().split() if token
            )
            for entry in configurable_client_entries:
                search_text = f"{entry.friendly} {entry.name}".casefold()
                config_rows[entry.name].set_visibility(
                    all(token in search_text for token in query_tokens)
                )

        def update_client_pack_changelog_draft(
            event: ModWebValueContainer,
        ) -> None:
            nonlocal client_pack_changelog_draft
            client_pack_changelog_draft = _value_as_text(event)

        def client_pack_automated_changelog_to_append() -> str:
            text = model.client_pack_automated_changelog.strip()
            if not text or text == "No automated client-pack changes detected.":
                return ""
            if text.startswith("Automated client-pack changelog is unavailable:"):
                return ""
            return text

        def client_pack_publish_changelog() -> str:
            changelog = client_pack_changelog_draft.strip()
            automated_changelog = client_pack_automated_changelog_to_append()
            if not automated_changelog:
                return changelog
            return f"{changelog}\n\n{automated_changelog}" if changelog else automated_changelog

        def client_pack_publish_reasons_from_automated_changelog(text: str) -> tuple[str, ...]:
            if not text:
                return ()
            reasons: list[str] = []
            current_heading: str | None = None
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line:
                    current_heading = None
                    continue
                if line.endswith(":"):
                    current_heading = line[:-1]
                    continue
                if line.startswith("- "):
                    item = line[2:].strip()
                    reasons.append(f"{current_heading}: {item}" if current_heading is not None else item)
                    continue
                current_heading = None
                reasons.append(line)
            return tuple(reasons)

        def client_pack_publish_reasons() -> tuple[str, ...]:
            automated_changelog = client_pack_automated_changelog_to_append()
            reasons: list[str] = []
            if model.client_pack_published_version is None:
                reasons.append("This client pack has not been published yet.")
            if model.client_pack_content_dirty:
                reasons.append("Saved client-pack configuration changes are waiting to be published.")
            reasons.extend(client_pack_publish_reasons_from_automated_changelog(automated_changelog))
            if not reasons:
                return ("No unpublished client-pack changes are currently detected.",)
            return tuple(dict.fromkeys(reasons))

        def client_pack_configuration_updates() -> tuple[tuple[NodeModEntry, ClientPackConfig], ...]:
            choice_groups = configured_choice_groups()
            for group_name, entries in choice_groups.items():
                if len(entries) < 2:
                    raise ValueError(f"Choice group {group_name!r} requires at least two mods.")
                selected_default = _value_as_text(config_default_selects[group_name])
                config_default_names[group_name] = selected_default

            updates: list[tuple[NodeModEntry, ClientPackConfig]] = []
            for entry in configurable_client_entries:
                policy = ClientPackPolicy(_value_as_text(config_policy_selects[entry.name]))
                if policy is ClientPackPolicy.REQUIRED:
                    client_pack = ClientPackConfig(
                        included_in_client=entry.client_pack.included_in_client,
                        policy=policy,
                    )
                elif policy is ClientPackPolicy.OPTIONAL:
                    client_pack = ClientPackConfig(
                        included_in_client=entry.client_pack.included_in_client,
                        policy=policy,
                        default_selected=(
                            entry.client_pack.default_selected
                            if entry.client_pack.policy is ClientPackPolicy.OPTIONAL
                            else True
                        ),
                    )
                else:
                    group_name = config_group_names[entry.name]
                    if not group_name:
                        raise ValueError(f"{entry.friendly} requires an alternative group ID.")
                    client_pack = ClientPackConfig(
                        included_in_client=entry.client_pack.included_in_client,
                        policy=policy,
                        choice_group=group_name,
                        default_choice=config_default_names.get(group_name) == entry.name,
                    )
                updates.append((entry, client_pack))
            return tuple(updates)

        def configured_client_pack_metadata() -> ClientPackMetadataConfig | None:
            if not is_minecraft_app:
                return None
            if (
                client_pack_name_input is None
                or client_pack_description_input is None
                or client_pack_filename_template_input is None
                or client_pack_include_servers_dat_checkbox is None
                or client_pack_include_options_txt_checkbox is None
            ):
                raise RuntimeError("Minecraft client-pack metadata controls are unavailable.")
            return ClientPackMetadataConfig(
                name=_value_as_text(client_pack_name_input),
                description=_value_as_text(client_pack_description_input),
                filename_template=_value_as_text(client_pack_filename_template_input),
                include_servers_dat=bool(_value_as_object(client_pack_include_servers_dat_checkbox)),
                include_options_txt=bool(_value_as_object(client_pack_include_options_txt_checkbox)),
            )

        async def persist_client_pack_configuration() -> None:
            updates = client_pack_configuration_updates()
            metadata = configured_client_pack_metadata()
            await self._remote_json_async(
                node=self._remote_node_link(model.node_name),
                app_name=model.app_name,
                path=f"/apps/{quote(model.app_name, safe='')}/mods/client-pack-config",
                scopes=(NodeApiScope.MODS_WRITE,),
                user=user,
                method="PUT",
                json_payload={
                    "mods": [
                        {
                            "mod_name": entry.name,
                            "client_pack": client_pack.model_dump(mode="json"),
                        }
                        for entry, client_pack in updates
                    ],
                    **(
                        {
                            "kubejs_scripts": [
                                {
                                    "relative_path": script.relative_path,
                                    "included": bool(
                                        _value_as_object(config_kubejs_script_checkboxes[script.relative_path])
                                    ),
                                }
                                for script in model.client_pack_kubejs_scripts
                            ]
                        }
                        if is_minecraft_app
                        else {}
                    ),
                    **({"metadata": metadata.model_dump(mode="json")} if metadata is not None else {}),
                },
            )
            self._backend.set_client_pack_changelog_draft(
                node_name=model.node_name,
                app_name=model.app_name,
                changelog=client_pack_changelog_draft,
            )

        async def _save_client_pack_configuration() -> None:
            try:
                await persist_client_pack_configuration()
            except Exception as xcp:
                log.warning("Client-pack configuration update failed for %s: %s", model.app_name, xcp)
                ui.notify(f"Client-pack configuration failed: {xcp}", type="negative", multi_line=True)
                return

            client_pack_config_dialog.close()
            ui.notify("Saved client-pack configuration.", type="positive")
            self._guarded_reload(ui=ui)

        async def save_client_pack_configuration() -> None:
            if client_pack_config_save_button is None:
                raise RuntimeError("Client-pack Save button was not rendered.")
            await self._run_with_loading_button(
                button=client_pack_config_save_button,
                action=_save_client_pack_configuration,
            )

        async def _publish_client_pack_configuration() -> None:
            changelog = client_pack_publish_changelog()
            if not changelog:
                ui.notify("Add a changelog before publishing the client pack.", type="warning")
                return
            try:
                await persist_client_pack_configuration()
                result = await self._remote_json_async(
                    node=self._remote_node_link(model.node_name),
                    app_name=model.app_name,
                    path=f"/apps/{quote(model.app_name, safe='')}/mods/client-pack-config/publish",
                    scopes=(NodeApiScope.MODS_WRITE,),
                    user=user,
                    method="POST",
                    json_payload={"changelog": changelog},
                )
            except Exception as xcp:
                log.warning("Client-pack publication failed for %s: %s", model.app_name, xcp)
                ui.notify(f"Client-pack publication failed: {xcp}", type="negative", multi_line=True)
                return

            client_pack_config_dialog.close()
            self._backend.clear_client_pack_changelog_draft(
                node_name=model.node_name,
                app_name=model.app_name,
            )
            ui.notify(str(result.get("message") or "Published client pack."), type="positive")
            self._guarded_reload(ui=ui)

        async def publish_client_pack_configuration() -> None:
            if client_pack_publish_button is None:
                raise RuntimeError("Client-pack Publish button was not rendered.")
            await self._run_with_loading_button(
                button=client_pack_publish_button,
                action=_publish_client_pack_configuration,
            )

        client_pack_config_dialog = ui.dialog()
        can_configure_client_pack: bool = supports_client_pack and self._user_has_level(
            user,
            Power_Level.admin,
        )
        client_pack_config_rendered = False

        def ensure_client_pack_config_dialog() -> None:
            nonlocal client_pack_changelog_input, client_pack_config_rendered
            nonlocal client_pack_description_input, client_pack_filename_template_input
            nonlocal client_pack_include_options_txt_checkbox
            nonlocal client_pack_include_servers_dat_checkbox
            nonlocal client_pack_config_save_button, client_pack_name_input
            nonlocal client_pack_publish_button

            def config_option_classes(policy: ClientPackPolicy) -> str:
                classes = "mod-client-pack-option mod-client-pack-config-option w-full items-center gap-2"
                if policy is ClientPackPolicy.ALTERNATIVE:
                    return f"{classes} mod-client-pack-config-option-alt"
                return classes

            if client_pack_config_rendered:
                return
            if not can_configure_client_pack:
                raise PermissionError("Client-pack configuration requires admin access.")
            client_pack_config_rendered = True
            with client_pack_config_dialog:
                with ui.card().classes("mod-card mod-dialog-card mod-client-pack-dialog-card"):
                    with ui.column().classes("mod-client-pack-body w-full"):
                        with ui.column().classes("mod-client-pack-header w-full"):
                            ui.label("Configure Client Pack").classes("text-xl font-black mod-title-small")
                            ui.label(
                                "Save configuration changes independently, then publish them with release notes."
                            ).classes("mod-subtitle text-sm")
                        with ui.row().classes("mod-client-pack-config-layout w-full"):
                            with ui.column().classes(
                                "mod-client-pack-config-column mod-client-pack-config-column-left"
                            ):
                                with ui.column().classes("mod-client-pack-section mod-client-pack-config-mods-section w-full"):
                                    ui.label("Mods").classes("mod-stat-label")
                                    ui.label("Choose which mods are required, optional, or alternatives.").classes(
                                        "mod-client-pack-section-hint mod-subtitle"
                                    )
                                    with ui.column().classes("mod-client-pack-config-mod-list w-full"):
                                        (
                                            ui.input(placeholder="Search pack mods")
                                            .props(
                                                "filled square dense clearable hide-bottom-space color=accent "
                                                f"debounce={_SEARCH_INPUT_DEBOUNCE_MILLISECONDS}"
                                            )
                                            .classes("mod-config-search mod-client-pack-config-search w-full")
                                            .on("update:model-value", filter_client_pack_config_rows)
                                        )
                                        with ui.column().classes("mod-client-pack-option-list w-full"):
                                            for entry in configurable_client_entries:
                                                with ui.row().classes(
                                                    config_option_classes(entry.client_pack.policy)
                                                ) as config_row:
                                                    config_rows[entry.name] = config_row
                                                    ui.label(entry.friendly).classes("mod-client-pack-option-label")
                                                    config_group_inputs[entry.name] = (
                                                        ui.input(
                                                            "Group ID",
                                                            value=config_group_names[entry.name],
                                                            placeholder="e.g. minimap",
                                                        )
                                                        .props(
                                                            "filled square dense hide-bottom-space color=accent debounce=150"
                                                        )
                                                        .classes(
                                                            "mod-config-input mod-client-pack-select "
                                                            "mod-client-pack-config-control mod-client-pack-config-group"
                                                        )
                                                    )
                                                    config_policy_selects[entry.name] = (
                                                        ui.select(
                                                            {policy.value: policy.label for policy in ClientPackPolicy},
                                                            value=entry.client_pack.policy.value,
                                                        )
                                                        .props(
                                                            "filled square dense hide-bottom-space color=accent options-dark"
                                                        )
                                                        .classes(
                                                            "mod-config-select mod-client-pack-select "
                                                            "mod-client-pack-config-control mod-client-pack-config-policy"
                                                        )
                                                    )

                            with ui.column().classes(
                                "mod-client-pack-config-column mod-client-pack-config-column-right"
                            ):

                                @ui.refreshable
                                def render_config_default_choices() -> None:
                                    config_default_selects.clear()
                                    groups = configured_choice_groups()
                                    if not groups:
                                        return
                                    with ui.column().classes("mod-client-pack-section w-full"):
                                        ui.label("Default alternatives").classes("mod-stat-label")
                                        ui.label("Choose the default mod for each alternative group.").classes(
                                            "mod-client-pack-section-hint mod-subtitle"
                                        )
                                        for group_name, entries in groups.items():
                                            member_names = {entry.name for entry in entries}
                                            default_name = config_default_names.get(group_name)
                                            if default_name not in member_names:
                                                default_name = entries[0].name
                                                config_default_names[group_name] = default_name
                                            config_default_selects[group_name] = (
                                                ui.select(
                                                    {entry.name: entry.friendly for entry in entries},
                                                    value=default_name,
                                                    label=group_name,
                                                )
                                                .props(
                                                    "filled square dense hide-bottom-space color=accent options-dark"
                                                )
                                                .classes("mod-config-select mod-client-pack-select w-full")
                                            )

                                if is_minecraft_app:
                                    with ui.column().classes("mod-client-pack-section w-full"):
                                        ui.label("Pack metadata").classes("mod-stat-label")
                                        ui.label(
                                            "Used for launcher manifests and the downloaded archive filename."
                                        ).classes("mod-client-pack-section-hint mod-subtitle")
                                        client_pack_name_input = (
                                            ui.input(
                                                "Name",
                                                value=client_pack_metadata.name,
                                            )
                                            .props(
                                                "filled square dense hide-bottom-space color=accent "
                                                f"maxlength={CLIENT_PACK_METADATA_NAME_MAX_LENGTH}"
                                            )
                                            .classes("w-full mod-config-input")
                                        )
                                        client_pack_description_input = (
                                            ui.textarea(
                                                "Description",
                                                value=client_pack_metadata.description,
                                            )
                                            .props(
                                                "filled square stack-label hide-bottom-space color=accent rows=2 "
                                                f"maxlength={CLIENT_PACK_METADATA_DESCRIPTION_MAX_LENGTH}"
                                            )
                                            .classes("w-full mod-config-input")
                                        )
                                        client_pack_filename_template_input = (
                                            ui.input(
                                                "Filename stem",
                                                value=client_pack_metadata.filename_template,
                                            )
                                            .props(
                                                "filled square dense hide-bottom-space color=accent "
                                                f"maxlength={CLIENT_PACK_FILENAME_TEMPLATE_MAX_LENGTH}"
                                            )
                                            .classes("w-full mod-config-input")
                                        )
                                        ui.label(
                                            "Placeholders: "
                                            + ", ".join(
                                                f"{{{placeholder}}}" for placeholder in CLIENT_PACK_FILENAME_PLACEHOLDERS
                                            )
                                            + ". The file extension is added automatically."
                                        ).classes("mod-client-pack-section-hint mod-subtitle text-xs")
                                        client_pack_include_servers_dat_checkbox = render_client_pack_file_option(
                                            label="Include servers.dat",
                                            value=client_pack_metadata.include_servers_dat,
                                            preview_path="overrides/servers.dat",
                                            preview_display_name="servers.dat",
                                        )
                                        client_pack_include_options_txt_checkbox = render_client_pack_file_option(
                                            label="Include options.txt",
                                            value=client_pack_metadata.include_options_txt,
                                            preview_path="overrides/options.txt",
                                            preview_display_name="options.txt",
                                        )
                                if is_minecraft_app:
                                    with ui.column().classes("mod-client-pack-section w-full"):
                                        ui.label("KubeJS scripts").classes("mod-stat-label")
                                        ui.label(
                                            "Choose which server and startup scripts are included in the client pack."
                                        ).classes("mod-client-pack-section-hint mod-subtitle")
                                        if model.client_pack_kubejs_scripts:
                                            with ui.column().classes("mod-client-pack-option-list w-full"):
                                                for script in model.client_pack_kubejs_scripts:
                                                    config_kubejs_script_checkboxes[script.relative_path] = (
                                                        ui.checkbox(
                                                            script.relative_path,
                                                            value=script.included,
                                                        )
                                                        .props("dense color=accent keep-color")
                                                        .classes("mod-client-pack-checkbox w-full")
                                                    )
                                        else:
                                            ui.label("No KubeJS server or startup scripts were found.").classes(
                                                "mod-client-pack-section-hint mod-subtitle text-sm"
                                            )
                                with ui.column().classes(
                                    "mod-client-pack-section mod-client-pack-release-section w-full"
                                ):
                                    ui.label("Release").classes("mod-stat-label")
                                    with ui.row().classes(
                                        "mod-client-pack-release-versions w-full flex-wrap"
                                    ):
                                        with ui.column().classes("mod-client-pack-release-version"):
                                            ui.label("Current version").classes("mod-subtitle text-xs")
                                            ui.label(
                                                model.client_pack_published_version or "Unpublished"
                                            ).classes("mod-stat-value")
                                        with ui.column().classes("mod-client-pack-release-version"):
                                            ui.label("Proposed next version").classes("mod-subtitle text-xs")
                                            ui.label(
                                                model.client_pack_next_version or "Unavailable"
                                            ).classes("mod-stat-value")
                                    with ui.column().classes("mod-client-pack-changelog-block w-full"):
                                        client_pack_changelog_input = (
                                            ui.textarea(
                                                "Changelog",
                                                value=client_pack_changelog_draft,
                                                placeholder="Describe client-pack changes in this release…",
                                                on_change=update_client_pack_changelog_draft,
                                            )
                                            .props(
                                                "filled square stack-label hide-bottom-space color=accent rows=3 "
                                                f"maxlength={CLIENT_PACK_CHANGELOG_MAX_LENGTH}"
                                            )
                                            .classes("w-full mod-config-input mod-client-pack-changelog")
                                        )
                                        ui.textarea(
                                            "Automated append",
                                            value=model.client_pack_automated_changelog
                                            or "No automated client-pack changes detected.",
                                        ).props(
                                            "filled square stack-label readonly hide-bottom-space color=accent rows=6"
                                        ).classes(
                                            "w-full mod-config-input mod-client-pack-changelog-automation"
                                        )
                                        ui.label(
                                            "Draft notes are shared when this configuration is saved."
                                        ).classes(
                                            "mod-client-pack-section-hint mod-client-pack-changelog-hint "
                                            "mod-subtitle text-xs"
                                        )
                                    with ui.column().classes("mod-client-pack-publish-reasons w-full"):
                                        ui.label("Publish reasons").classes("mod-stat-label")
                                        for reason in client_pack_publish_reasons():
                                            ui.label(reason).classes("mod-client-pack-publish-reason")
                                    with ui.row().classes("mod-client-pack-actions w-full"):
                                        ui.button("Cancel", on_click=client_pack_config_dialog.close).classes(
                                            "mod-list-button secondary"
                                        )
                                        client_pack_config_save_button = ui.button(
                                            "Save",
                                            on_click=save_client_pack_configuration,
                                        ).classes("mod-list-button secondary")
                                        client_pack_publish_button = ui.button(
                                            "Publish",
                                            on_click=publish_client_pack_configuration,
                                        ).classes("mod-list-button")

                        def refresh_config_row(entry: NodeModEntry) -> None:
                            is_alternative = (
                                ClientPackPolicy(_value_as_text(config_policy_selects[entry.name]))
                                is ClientPackPolicy.ALTERNATIVE
                            )
                            config_group_inputs[entry.name].set_visibility(is_alternative)
                            config_rows[entry.name].classes(
                                config_option_classes(
                                    ClientPackPolicy.ALTERNATIVE if is_alternative else ClientPackPolicy.REQUIRED
                                )
                            )
                            render_config_default_choices.refresh()

                        def create_config_policy_handler(
                            entry: NodeModEntry,
                        ) -> Callable[[ModWebEventArgumentsContainer], None]:
                            def handle_policy_change(_event: ModWebEventArgumentsContainer) -> None:
                                refresh_config_row(entry)

                            return handle_policy_change

                        def create_config_group_handler(
                            entry: NodeModEntry,
                        ) -> Callable[[ModWebEventArgumentsContainer], None]:
                            def handle_group_change(event: ModWebEventArgumentsContainer) -> None:
                                raw_group_name = _event_args_as_text(event)
                                config_group_names[entry.name] = raw_group_name
                                invalid = any(character.isspace() for character in raw_group_name)
                                if invalid:
                                    config_group_inputs[entry.name].classes(
                                        add="mod-client-pack-config-invalid"
                                    )
                                else:
                                    config_group_inputs[entry.name].classes(
                                        remove="mod-client-pack-config-invalid"
                                    )
                                render_config_default_choices.refresh()

                            return handle_group_change

                        for entry in configurable_client_entries:
                            config_policy_selects[entry.name].on(
                                "update:model-value",
                                create_config_policy_handler(entry),
                            )
                            config_group_inputs[entry.name].on(
                                "update:model-value",
                                create_config_group_handler(entry),
                            )
                            config_group_inputs[entry.name].set_visibility(
                                entry.client_pack.policy is ClientPackPolicy.ALTERNATIVE
                            )
                        render_config_default_choices()

        def open_client_pack_configuration() -> None:
            nonlocal client_pack_changelog_draft
            ensure_client_pack_config_dialog()
            shared_draft = self._backend.client_pack_changelog_draft(
                node_name=model.node_name,
                app_name=model.app_name,
            )
            if shared_draft is not None:
                client_pack_changelog_draft = shared_draft
                if client_pack_changelog_input is not None:
                    client_pack_changelog_input.set_value(shared_draft)
            client_pack_config_dialog.open()

        client_pack_release_changes: tuple[tuple[str, str], ...] = tuple(
            (release.version, release.changelog) for release in model.client_pack_releases
        )
        if not client_pack_release_changes and (
            model.client_pack_published_version is not None
            and model.client_pack_published_changelog is not None
        ):
            client_pack_release_changes = (
                (
                    model.client_pack_published_version,
                    model.client_pack_published_changelog,
                ),
            )

        client_pack_changes_dialog = ui.dialog()
        client_pack_changes_rendered = False

        def ensure_client_pack_changes_dialog() -> None:
            nonlocal client_pack_changes_rendered
            if client_pack_changes_rendered:
                return
            if not supports_client_pack:
                raise RuntimeError("Client-pack changes are unavailable for this app.")
            client_pack_changes_rendered = True
            with client_pack_changes_dialog:
                with ui.card().classes("mod-card mod-dialog-card mod-client-pack-dialog-card"):
                    with ui.column().classes("mod-client-pack-body w-full"):
                        with ui.column().classes("mod-client-pack-header w-full"):
                            ui.label("Client Pack Changes").classes("text-xl font-black mod-title-small")
                            ui.label("Published versions, newest first.").classes("mod-subtitle text-sm")
                        if client_pack_release_changes:
                            for version, changelog in reversed(client_pack_release_changes):
                                with ui.column().classes("mod-client-pack-section w-full"):
                                    ui.label(version).classes("mod-stat-label")
                                    ui.label(changelog).classes("mod-client-pack-changelog-content")
                        else:
                            ui.label("No changelog is available.").classes(
                                "mod-client-pack-changelog-content"
                            )
                        with ui.row().classes("mod-client-pack-actions w-full"):
                            ui.button("Close", on_click=client_pack_changes_dialog.close).classes(
                                "mod-list-button secondary"
                            )

        def open_client_pack_changes_dialog() -> None:
            ensure_client_pack_changes_dialog()
            client_pack_changes_dialog.open()

        client_pack_dialog = ui.dialog()
        client_pack_dialog_rendered = False

        def open_client_pack_dialog() -> None:
            nonlocal client_pack_dialog_rendered
            if not client_pack_dialog_rendered:
                if not supports_client_pack:
                    raise RuntimeError("Client packs are unavailable for this app.")
                client_pack_dialog_rendered = True
                render_client_pack_dialog()
            client_pack_dialog.open()

        def render_client_pack_dialog() -> None:
            nonlocal client_pack_format_select, include_kubejs_scripts_checkbox
            nonlocal include_options_txt_checkbox, include_servers_dat_checkbox
            with client_pack_dialog:
                with ui.card().classes("mod-card mod-dialog-card mod-client-pack-dialog-card"):
                    with ui.column().classes("mod-client-pack-body w-full"):
                        with ui.column().classes("mod-client-pack-header w-full"):
                            ui.label("Download Client Pack").classes("text-xl font-black mod-title-small")
                            ui.label(
                                "Required mods are always included. Choose which optional mods to add."
                            ).classes("mod-subtitle text-sm")
                        if capabilities.supports_launcher_formats:
                            with ui.column().classes("mod-client-pack-section w-full"):
                                ui.label("Pack format").classes("mod-stat-label")
                                client_pack_format_select = ui.select(
                                    self._client_pack_format_options(model.app_scope),
                                    value=self._default_client_pack_format(model.app_scope).value,
                                ).props("filled square dense hide-bottom-space color=accent options-dark").classes(
                                    "mod-config-select mod-client-pack-select w-full"
                                )
                                if is_minecraft_app:
                                    ui.label(
                                        "If you choose CurseForge and it warns about bundled local files, choose "
                                        "All Files only when you obtained the ZIP from this trusted server."
                                    ).classes("mod-client-pack-section-hint mod-subtitle text-xs")
                        if show_client_pack_kubejs_toggle:
                            with ui.column().classes("mod-client-pack-section w-full"):
                                ui.label("KubeJS").classes("mod-stat-label")
                                include_kubejs_scripts_checkbox = (
                                    ui.checkbox(
                                        "Include configured KubeJS scripts",
                                        value=True,
                                    )
                                    .props("dense color=accent keep-color")
                                    .classes("mod-client-pack-checkbox w-full")
                                )
                        if is_minecraft_app:
                            with ui.column().classes("mod-client-pack-section w-full"):
                                ui.label("Client files").classes("mod-stat-label")
                                include_servers_dat_checkbox = render_client_pack_file_option(
                                    label="Include servers.dat",
                                    value=client_pack_metadata.include_servers_dat,
                                    preview_path="overrides/servers.dat",
                                    preview_display_name="servers.dat",
                                )
                                include_options_txt_checkbox = render_client_pack_file_option(
                                    label="Include options.txt",
                                    value=client_pack_metadata.include_options_txt,
                                    preview_path="overrides/options.txt",
                                    preview_display_name="options.txt",
                                )
                        required_entries: tuple[NodeModEntry, ...] = tuple(
                            entry
                            for entry in model.mods.mods
                            if entry.client_pack_eligible and entry.client_pack.policy is ClientPackPolicy.REQUIRED
                        )
                        if optional_client_entries:
                            with ui.column().classes("mod-client-pack-section w-full"):
                                ui.label("Optional mods").classes("mod-stat-label")
                                ui.label("Checked mods will be included in the pack.").classes(
                                    "mod-client-pack-section-hint mod-subtitle"
                                )
                                with ui.column().classes("mod-client-pack-option-list w-full"):
                                    for entry in optional_client_entries:
                                        optional_client_checkboxes[entry.name] = (
                                            ui.checkbox(
                                                entry.friendly,
                                                value=entry.client_pack.default_selected,
                                            )
                                            .props("dense color=accent keep-color")
                                            .classes("mod-client-pack-checkbox w-full")
                                        )
                        if client_choice_groups:
                            with ui.column().classes("mod-client-pack-section w-full"):
                                ui.label("Choose one").classes("mod-stat-label")
                                ui.label("Select one mutually exclusive option from each group.").classes(
                                    "mod-client-pack-section-hint mod-subtitle"
                                )
                                with ui.column().classes("mod-client-pack-choice-list w-full"):
                                    for group_name, choices in client_choice_groups.items():
                                        group_label: str = (
                                            group_name.replace("_", " ").replace("-", " ").strip().title()
                                        )
                                        with ui.column().classes("mod-client-pack-choice w-full"):
                                            client_choice_selects[group_name] = (
                                                ui.select(
                                                    {entry.name: entry.friendly for entry in choices},
                                                    value=client_choice_defaults[group_name],
                                                    label=group_label,
                                                )
                                                .props(
                                                    "filled square dense hide-bottom-space color=accent options-dark"
                                                )
                                                .classes("mod-config-select mod-client-pack-select w-full")
                                            )
                        with ui.row().classes("mod-client-pack-actions w-full"):
                            ui.button("Cancel", on_click=client_pack_dialog.close).classes("mod-list-button secondary")
                            ui.button("Changes", on_click=open_client_pack_changes_dialog).classes(
                                "mod-list-button secondary"
                            )
                            client_pack_download_button = ui.button(
                                "Download",
                                on_click=download_configured_client_pack,
                            ).classes("mod-list-button")
                            client_pack_download_button.set_enabled(not model.client_pack_content_dirty)
                        if model.client_pack_content_dirty:
                            ui.label(
                                "This client pack has unpublished changes. Publish them before downloading."
                            ).classes("mod-client-pack-section-hint mod-subtitle text-sm")
                        if required_entries:
                            with ui.column().classes("mod-client-pack-section w-full"):
                                ui.label("Required").classes("mod-stat-label")
                                ui.label("These mods are always included.").classes(
                                    "mod-client-pack-section-hint mod-subtitle"
                                )
                                if len(required_entries) >= _VIRTUALIZED_LIST_MIN_ITEMS:
                                    required_rows: list[dict[object, object]] = [
                                        {
                                            "name": entry.name,
                                            "friendly": entry.friendly,
                                        }
                                        for entry in required_entries
                                    ]
                                    (
                                        ui.table(
                                            rows=required_rows,
                                            columns=[
                                                {
                                                    "name": "friendly",
                                                    "label": "Mod",
                                                    "field": "friendly",
                                                    "align": "left",
                                                },
                                                {
                                                    "name": "name",
                                                    "label": "File",
                                                    "field": "name",
                                                    "align": "left",
                                                },
                                            ],
                                            row_key="name",
                                            pagination=0,
                                        )
                                        .props(
                                            'flat dark virtual-scroll virtual-scroll-item-size=42 '
                                            ':rows-per-page-options="[0]"'
                                        )
                                        .classes("mod-virtual-list mod-client-pack-required-table w-full")
                                    )
                                else:
                                    with ui.column().classes("mod-client-pack-option-list w-full"):
                                        for entry in required_entries:
                                            with ui.row().classes("mod-client-pack-option w-full items-center"):
                                                ui.label(entry.friendly).classes("mod-client-pack-option-label")

        modlist_dialog = ui.dialog()
        with modlist_dialog:
            with ui.card().classes("mod-card mod-dialog-card mod-modlist-dialog-card"):
                with ui.column().classes("mod-modlist-body w-full"):
                    with ui.column().classes("gap-1 w-full"):
                        ui.label("Modlist").classes("text-xl font-black mod-title-small")
                        ui.label("Format and copy the current indexed mod list.").classes("mod-subtitle text-sm")
                    modlist_format_select = (
                        ui.select(
                            {output_format.value: output_format.label for output_format in ModWebModlistFormat},
                            value=ModWebModlistFormat.PLAINTEXT.value,
                            label="Format",
                        )
                        .props("filled square dense hide-bottom-space color=accent options-dark")
                        .classes("mod-config-select mod-modlist-format w-full")
                    )
                    with ui.row().classes("mod-modlist-options w-full flex-wrap"):
                        modlist_include_version = (
                            ui.checkbox("Version", value=True)
                            .props("dense color=accent keep-color")
                            .classes("mod-client-pack-checkbox mod-modlist-toggle")
                        )
                        modlist_include_filename = (
                            ui.checkbox("Filename", value=False)
                            .props("dense color=accent keep-color")
                            .classes("mod-client-pack-checkbox mod-modlist-toggle")
                        )
                    with ui.row().classes("mod-modlist-options w-full flex-wrap"):
                        modlist_include_disabled = (
                            ui.checkbox("Disabled", value=False)
                            .props("dense color=accent keep-color")
                            .classes("mod-client-pack-checkbox mod-modlist-toggle")
                        )
                        modlist_include_builtin = (
                            ui.checkbox("Built-in", value=False)
                            .props("dense color=accent keep-color")
                            .classes("mod-client-pack-checkbox mod-modlist-toggle")
                        )
                        modlist_include_client = (
                            ui.checkbox("Client", value=True)
                            .props("dense color=accent keep-color")
                            .classes("mod-client-pack-checkbox mod-modlist-toggle")
                        )

                    def current_modlist_text() -> str:
                        return self._render_modlist(
                            model.mods.mods,
                            instance_name=model.app_friendly,
                            pack_version=self._supported_client_pack_version(model),
                            output_format=ModWebModlistFormat(_value_as_text(modlist_format_select)),
                            include_version=bool(_value_as_object(modlist_include_version)),
                            include_filename=bool(_value_as_object(modlist_include_filename)),
                            include_pack_version=self._supports_client_pack(model),
                            include_disabled=bool(_value_as_object(modlist_include_disabled)),
                            include_builtin=bool(_value_as_object(modlist_include_builtin)),
                            include_client=bool(_value_as_object(modlist_include_client)),
                        )

                    def copy_modlist() -> None:
                        if copy_text_to_clipboard(
                            ui=ui,
                            text=current_modlist_text(),
                            empty_message="There are no mods to copy.",
                        ):
                            ui.notify("Copied modlist to the clipboard.", type="positive")

                    with ui.column().classes("mod-modlist-preview-section w-full"):
                        ui.label("Preview").classes("mod-stat-label")
                        with ui.element("div").classes("mod-modlist-preview-frame"):

                            @ui.refreshable
                            def render_modlist_preview() -> None:
                                ui.label(current_modlist_text() or "No mods are currently indexed.").classes(
                                    "mod-modlist-preview w-full"
                                )

                            render_modlist_preview()

                    def refresh_modlist_preview(_event: ModWebEventArgumentsContainer) -> None:
                        render_modlist_preview.refresh()

                    modlist_format_select.on("update:model-value", refresh_modlist_preview)
                    modlist_include_version.on("update:model-value", refresh_modlist_preview)
                    modlist_include_filename.on("update:model-value", refresh_modlist_preview)
                    modlist_include_disabled.on("update:model-value", refresh_modlist_preview)
                    modlist_include_builtin.on("update:model-value", refresh_modlist_preview)
                    modlist_include_client.on("update:model-value", refresh_modlist_preview)
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("Copy", on_click=copy_modlist).classes("mod-list-button")
                        ui.button("Close", on_click=modlist_dialog.close).classes("mod-list-button secondary")

        async def find_bulk_mod_metadata(operation_id: str) -> None:
            set_metadata_status("Scanning…", running=True)
            log.info(
                "Bulk mod metadata discovery started from dashboard: node=%s app=%s operation=%s",
                model.node_name,
                model.app_name,
                operation_id,
            )
            try:
                ui.notify("Scanning local mod identities and provider metadata…", type="info")
                discovery = await self._discover_bulk_mod_metadata(
                    model=model,
                    operation_id=operation_id,
                    user=user,
                )
            except Exception as xcp:
                if metadata_cancel_requested:
                    raise asyncio.CancelledError() from xcp
                log.warning(
                    "Bulk mod metadata discovery failed: node=%s app=%s operation=%s error=%s",
                    model.node_name,
                    model.app_name,
                    operation_id,
                    xcp,
                )
                set_metadata_status("Failed", running=False)
                ui.notify(f"Bulk metadata discovery failed: {xcp}", type="negative")
                return

            entry_by_name = {entry.mod_name: entry for entry in discovery.entries}
            rows: list[_BulkMetadataRow] = [
                {
                    "name": entry.mod_name,
                    "friendly": entry.friendly_name,
                    "status": entry.status.label,
                    "providers": ", ".join(
                        launcher_provider_label(provider)
                        for provider in entry.matched_providers
                    ) or "—",
                    "suggested_type": (
                        "—" if entry.suggested_mod_type is None else entry.suggested_mod_type.label
                    ),
                    "suggested_type_selectable": (
                        entry.status is BulkLauncherMetadataStatus.EXACT
                        and entry.suggested_mod_type is not None
                        and entry.suggested_mod_type is not ModType.REGULAR
                    ),
                    "apply_suggested_type": False,
                }
                for entry in discovery.entries
            ]
            exact_rows = [
                row
                for row in rows
                if entry_by_name[row["name"]].status is BulkLauncherMetadataStatus.EXACT
            ]
            set_metadata_status(
                f"{len(exact_rows)} exact"
                if exact_rows
                else "No exact matches",
                running=False,
            )
            log.info(
                "Bulk mod metadata discovery ready for review: node=%s app=%s operation=%s "
                "exact=%s unmatched=%s provider_errors=%s",
                model.node_name,
                model.app_name,
                operation_id,
                len(exact_rows),
                len(rows) - len(exact_rows),
                len(discovery.provider_errors),
            )
            review_dialog = ui.dialog()
            apply_button: Button | None = None
            result_table: Table | None = None
            apply_suggested_type_mod_names: set[str] = set()

            def update_type_selection(event: ModWebEventArgumentsContainer) -> None:
                raw_args = event.args
                if not isinstance(raw_args, Mapping):
                    raise ValueError("Bulk metadata type selection event must contain a mapping.")
                args = cast(Mapping[str, object], raw_args)
                mod_name = args.get("name")
                selected = args.get("selected")
                if not isinstance(mod_name, str) or not mod_name:
                    raise ValueError("Bulk metadata type selection requires a mod name.")
                if not isinstance(selected, bool):
                    raise ValueError("Bulk metadata type selection requires a boolean state.")
                entry = entry_by_name.get(mod_name)
                if (
                    entry is None
                    or entry.status is not BulkLauncherMetadataStatus.EXACT
                    or entry.suggested_mod_type is None
                    or entry.suggested_mod_type is ModType.REGULAR
                ):
                    raise ValueError(
                        "Only non-Regular type suggestions for exact matches can be selected."
                    )
                row = next((candidate for candidate in rows if candidate["name"] == mod_name), None)
                if row is None:
                    raise RuntimeError("Bulk metadata type selection row was not rendered.")
                row["apply_suggested_type"] = selected
                if selected:
                    apply_suggested_type_mod_names.add(mod_name)
                else:
                    apply_suggested_type_mod_names.discard(mod_name)
                log.info(
                    "Bulk mod metadata type selection changed: node=%s app=%s "
                    "discovery_operation=%s mod=%s selected=%s suggested_type=%s",
                    model.node_name,
                    model.app_name,
                    operation_id,
                    mod_name,
                    selected,
                    entry.suggested_mod_type.value,
                )

            def enforce_exact_metadata_selection(
                event: "TableSelectionEventArguments",
            ) -> None:
                if result_table is None:
                    return
                raw_selection = cast(list[object], cast(object, event.selection))
                exact_selection: list[_BulkMetadataRow] = []
                for raw_row in raw_selection:
                    row = cast(Mapping[str, object], raw_row)
                    mod_name = str(row.get("name", ""))
                    entry = entry_by_name.get(mod_name)
                    if entry is None or entry.status is not BulkLauncherMetadataStatus.EXACT:
                        continue
                    exact_selection.append(cast(_BulkMetadataRow, raw_row))
                result_table.selected = cast(
                    list[dict[object, object]],
                    cast(object, exact_selection),
                )
                result_table.update()
                if apply_button is not None:
                    apply_button.set_text(
                        f"Apply {len(exact_selection)} Exact Matches"
                    )
                    apply_button.set_enabled(bool(exact_selection))

            def apply_selected_metadata() -> None:
                if result_table is None or apply_button is None:
                    raise RuntimeError("Bulk metadata review controls were not rendered.")
                selected_names = tuple(
                    str(row["name"])
                    for row in result_table.selected
                    if entry_by_name[str(row["name"])].status
                    is BulkLauncherMetadataStatus.EXACT
                )
                selected_name_set = frozenset(selected_names)
                selected_type_names = tuple(
                    row["name"]
                    for row in rows
                    if row["name"] in selected_name_set
                    and row["name"] in apply_suggested_type_mod_names
                )

                async def apply(apply_operation_id: str) -> None:
                    set_metadata_status("Applying…", running=True)
                    log.info(
                        "Bulk mod metadata apply started from dashboard: node=%s app=%s "
                        "operation=%s selected=%s type_selections=%s",
                        model.node_name,
                        model.app_name,
                        apply_operation_id,
                        len(selected_names),
                        len(selected_type_names),
                    )
                    try:
                        result = await self._apply_bulk_mod_metadata(
                            model=model,
                            operation_id=apply_operation_id,
                            discovery_operation_id=operation_id,
                            mod_names=selected_names,
                            apply_suggested_type_mod_names=selected_type_names,
                            user=user,
                        )
                    except Exception as xcp:
                        if metadata_cancel_requested:
                            raise asyncio.CancelledError() from xcp
                        log.warning(
                            "Bulk mod metadata apply failed: node=%s app=%s operation=%s error=%s",
                            model.node_name,
                            model.app_name,
                            apply_operation_id,
                            xcp,
                        )
                        set_metadata_status("Apply failed", running=False)
                        ui.notify(f"Bulk metadata update failed: {xcp}", type="negative")
                        return
                    review_dialog.close()
                    applied_count = len(result.applied_mod_names)
                    applied_type_count = len(result.applied_type_mod_names)
                    set_metadata_status(
                        f"Applied {applied_count} / {applied_type_count} types",
                        running=False,
                    )
                    log.info(
                        "Bulk mod metadata apply completed in dashboard: node=%s app=%s "
                        "operation=%s applied=%s types_updated=%s",
                        model.node_name,
                        model.app_name,
                        apply_operation_id,
                        applied_count,
                        applied_type_count,
                    )
                    ui.notify(
                        f"Applied exact metadata to {applied_count} mod"
                        f"{'s' if applied_count != 1 else ''}; updated "
                        f"{applied_type_count} type"
                        f"{'s' if applied_type_count != 1 else ''}.",
                        type="positive",
                    )
                    self._guarded_reload(ui=ui)

                async def run_apply(operation_id: str) -> None:
                    await self._run_with_loading_button(
                        button=apply_button,
                        action=lambda: apply(operation_id),
                    )

                start_metadata_operation(run_apply)

            with review_dialog:
                with ui.card().classes(
                    "mod-card mod-dialog-card mod-bulk-metadata-dialog-card"
                ):
                    with ui.column().classes("w-full gap-4"):
                        with ui.column().classes("gap-1 w-full"):
                            ui.label("Bulk Metadata Review").classes(
                                "text-xl font-black mod-title-small"
                            )
                            ui.label(
                                f"{len(exact_rows)} exact matches; "
                                f"{len(rows) - len(exact_rows)} unmatched. "
                                "Only exact file identities are selectable. Type changes are optional "
                                "and unchecked by default."
                            ).classes("mod-subtitle text-sm")
                        for provider_error in discovery.provider_errors:
                            ui.label(
                                f"{launcher_provider_label(provider_error.provider)}: "
                                f"{provider_error.message}"
                            ).classes("mod-subtitle text-sm text-warning")
                        result_table = (
                            ui.table(
                                rows=rows,
                                columns=[
                                    {
                                        "name": "friendly",
                                        "label": "Mod",
                                        "field": "friendly",
                                        "align": "left",
                                    },
                                    {
                                        "name": "status",
                                        "label": "Match",
                                        "field": "status",
                                        "align": "left",
                                    },
                                    {
                                        "name": "providers",
                                        "label": "Providers",
                                        "field": "providers",
                                        "align": "left",
                                    },
                                    {
                                        "name": "suggested_type",
                                        "label": "Type suggestion",
                                        "field": "suggested_type",
                                        "align": "left",
                                    },
                                ],
                                row_key="name",
                                selection="multiple",
                                pagination=0,
                                on_select=enforce_exact_metadata_selection,
                            )
                            .props(
                                'flat dark virtual-scroll virtual-scroll-item-size=48 '
                                ':rows-per-page-options="[0]"'
                            )
                            .classes("mod-bulk-metadata-table w-full")
                        )
                        result_table.selected = cast(
                            list[dict[object, object]],
                            cast(object, exact_rows),
                        )
                        result_table.add_slot(
                            "header-selection",
                            """
                            <q-checkbox v-model="props.selected"
                                        class="mod-bulk-metadata-selection-checkbox" />
                            """,
                        )
                        result_table.add_slot(
                            "body-selection",
                            """
                            <q-checkbox v-model="props.selected" @click.stop
                                        class="mod-bulk-metadata-selection-checkbox" />
                            """,
                        )
                        result_table.add_slot(
                            "body-cell-suggested_type",
                            """
                            <q-td :props="props">
                              <div class="mod-bulk-metadata-type-suggestion">
                                <q-checkbox v-if="props.row.suggested_type_selectable"
                                            v-model="props.row.apply_suggested_type"
                                            @click.stop
                                            @update:model-value="$parent.$emit('bulk-type-selection', {name: props.row.name, selected: $event})"
                                            class="mod-bulk-metadata-type-checkbox" />
                                <span>{{ props.row.suggested_type }}</span>
                              </div>
                            </q-td>
                            """,
                        )
                        result_table.on("bulk-type-selection", update_type_selection)
                        with ui.row().classes("w-full justify-end gap-2"):
                            ui.button("Cancel", on_click=review_dialog.close).classes(
                                "mod-list-button secondary"
                            )
                            apply_button = ui.button(
                                f"Apply {len(exact_rows)} Exact Matches",
                                on_click=apply_selected_metadata,
                            ).classes("mod-list-button")
                            apply_button.set_enabled(bool(exact_rows))
            review_dialog.open()

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
                        delete_selected_button = ui.button(
                            "Delete",
                            on_click=delete_selected,
                        ).classes("mod-list-button danger")

        with ui.card().classes(self._flat_tab_card_classes()) as rendered_metadata_container:
            metadata_ui_container = rendered_metadata_container
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
                    upload_picker_action = open_upload_picker

                @ui.refreshable
                def _mod_download_rows(search_query: str) -> None:
                    nonlocal virtual_mod_rows, virtual_mod_table
                    checkboxes.clear()
                    virtual_mod_table = None
                    virtual_mod_rows = []
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

                    if len(filtered_mods) >= _VIRTUALIZED_LIST_MIN_ITEMS and callable(
                        getattr(ui, "table", None)
                    ):
                        mod_by_name: dict[str, NodeModEntry] = {
                            entry.name: entry for entry in filtered_mods
                        }
                        rows: list[_VirtualModRow] = [
                            {
                                "name": entry.name,
                                "friendly": entry.friendly,
                                "file": entry.name,
                                "size": entry.size_text,
                                "update_available": entry.name in available_update_mod_names,
                                "placement": entry.placement.label,
                                "policy": (
                                    ""
                                    if entry.client_pack.policy is ClientPackPolicy.REQUIRED
                                    else entry.client_pack.policy.label
                                ),
                                "type": entry.mod_type.label,
                                "type_tone": self._mod_type_badge_tone(entry.mod_type),
                                "downloadable": capabilities.supports_raw_download and entry.downloadable,
                                "download_block_label": entry.download_block_label or "Not downloadable",
                                "selectable": entry.name in selectable_name_set,
                                "show_download_block": not entry.downloadable
                                and not (
                                    entry.mod_type is ModType.SERVER
                                    and entry.download_block_reason == ModDownloadBlockReason.SERVER_ONLY.value
                                ),
                                "show_placement": entry.placement is ModPlacement.CLIENT_ONLY,
                                "show_policy": entry.client_pack.policy is not ClientPackPolicy.REQUIRED,
                                "state_class": (
                                    "blocked"
                                    if not entry.downloadable
                                    else (
                                        "mod-row-disabled"
                                        if entry.placement is ModPlacement.SERVER_DISABLED
                                        else ""
                                    )
                                ),
                            }
                            for entry in filtered_mods
                        ]
                        columns: list[dict[str, object]] = [
                            {"name": "friendly", "label": "Mod", "field": "friendly", "align": "left"},
                        ]

                        def sync_virtual_selection(event: "TableSelectionEventArguments") -> None:
                            visible_selectable_names: set[str] = {
                                row["name"] for row in rows if row["selectable"]
                            }
                            selected_mod_names.difference_update(visible_selectable_names)
                            selection_rows: list[object] = cast(
                                list[object],
                                cast(object, event.selection),
                            )
                            for raw_row in selection_rows:
                                row: Mapping[str, object] = cast(Mapping[str, object], raw_row)
                                mod_name: str = str(row.get("name", ""))
                                if mod_name in visible_selectable_names:
                                    selected_mod_names.add(mod_name)
                            update_count()

                        opened_dialogs: dict[str, Dialog] = {}

                        def open_virtual_mod(mod_name: str) -> None:
                            entry: NodeModEntry | None = mod_by_name.get(mod_name)
                            if entry is None:
                                return
                            dialog = opened_dialogs.get(mod_name)
                            if dialog is None:
                                dialog = self._render_mod_info_dialog(
                                    ui=ui,
                                    entry=entry,
                                    model=model,
                                    user=user,
                                )
                                opened_dialogs[mod_name] = dialog
                            dialog.open()

                        async def handle_virtual_mod_action(event: ModWebEventArgumentsContainer) -> None:
                            raw_args: object = getattr(event, "args", None)
                            if not isinstance(raw_args, Mapping):
                                return
                            payload: Mapping[str, object] = cast(Mapping[str, object], raw_args)
                            action_value: object = payload.get("action")
                            if action_value not in ("details", "download"):
                                return
                            action: _VirtualModAction = action_value
                            mod_name: str = str(payload.get("name", "")).strip()
                            if action == "details":
                                open_virtual_mod(mod_name)
                                return
                            entry: NodeModEntry | None = mod_by_name.get(mod_name)
                            download_url: str | None = model.mod_download_urls.get(mod_name)
                            if entry is None or download_url is None:
                                return
                            await self._start_download(
                                ui=ui,
                                user=user,
                                model=model,
                                url=download_url,
                                message=self._download_feedback_message(
                                    kind=ModDownloadKind.SINGLE,
                                    app_friendly=model.app_friendly,
                                    mod_friendly=entry.friendly,
                                ),
                                filenames=(entry.name,),
                            )

                        virtual_mod_table = (
                            ui.table(
                                rows=cast(list[dict[object, object]], cast(object, rows)),
                                columns=columns,
                                row_key="name",
                                selection="multiple",
                                pagination=0,
                                on_select=sync_virtual_selection,
                            )
                            .props(
                                'flat dark hide-header hide-bottom virtual-scroll virtual-scroll-item-size=76 '
                                ':rows-per-page-options="[0]"'
                            )
                            .classes("mod-virtual-list mod-virtual-mod-table w-full")
                        )
                        virtual_mod_rows = rows
                        initially_selected_rows = [
                            row for row in rows if str(row["name"]) in selected_mod_names
                        ]
                        virtual_mod_table.selected = cast(
                            list[dict[object, object]],
                            cast(object, initially_selected_rows),
                        )
                        virtual_mod_table.add_slot(
                            "body",
                            """
                            <q-tr :props="props" class="mod-virtual-row">
                              <q-td :colspan="props.cols.length + 1" class="mod-virtual-row-cell">
                                <div :class="['mod-row', 'mod-row-clickable', props.row.state_class]"
                                     :data-mod-name="props.row.name">
                                  <q-checkbox v-if="props.row.selectable" v-model="props.selected"
                                              dense @click.stop />
                                  <q-checkbox v-else :model-value="false" dense disable @click.stop />
                                  <div class="mod-row-main">
                                    <div class="mod-row-title">{{ props.row.friendly }}</div>
                                    <div class="mod-row-file">{{ props.row.file }}</div>
                                  </div>
                                  <div class="mod-row-meta">
                                    <span class="mod-pill size">{{ props.row.size }}</span>
                                    <span v-if="props.row.update_available" class="mod-pill size update">Update</span>
                                    <span v-if="props.row.show_placement" class="mod-pill">
                                      {{ props.row.placement }}
                                    </span>
                                    <span v-if="props.row.show_policy" class="mod-pill">
                                      {{ props.row.policy }}
                                    </span>
                                    <span v-if="props.row.show_download_block" class="mod-pill blocked">
                                      {{ props.row.download_block_label }}
                                    </span>
                                  </div>
                                  <q-btn v-if="props.row.downloadable" flat dense no-caps label="Download"
                                         class="mod-row-download" data-mod-download />
                                  <span v-else class="mod-row-download blocked">Blocked</span>
                                  <div class="mod-setting-badge-rail mod-mod-type-badge-rail">
                                    <span :class="['mod-badge', props.row.type_tone,
                                                   'mod-setting-badge', 'mod-mod-type-badge']">
                                      {{ props.row.type }}
                                    </span>
                                  </div>
                                </div>
                              </q-td>
                            </q-tr>
                            """,
                        )
                        virtual_mod_table.on(
                            "click",
                            handle_virtual_mod_action,
                            js_handler="""
                            (event) => {
                              const target = event.target instanceof Element ? event.target : null;
                              const row = target?.closest('[data-mod-name]');
                              if (!row) return;
                              emit({
                                action: target.closest('[data-mod-download]') ? 'download' : 'details',
                                name: row.dataset.modName,
                              });
                            }
                            """,
                        )
                        self._restore_virtual_mod_scroll_position(
                            ui=ui,
                            node_name=model.node_name,
                            app_name=model.app_name,
                        )
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
                                download_url=(
                                    model.mod_download_urls.get(entry.name)
                                    if capabilities.supports_raw_download
                                    else None
                                ),
                                on_change=_create_mod_selection_handler(entry.name),
                                can_select=entry.name in selectable_name_set,
                                app_friendly=model.app_friendly,
                                model=model,
                                user=user,
                                has_update=entry.name in available_update_mod_names,
                            )
                            if checkbox is not None:
                                checkboxes[entry.name] = checkbox
                                checkbox.set_value(entry.name in selected_mod_names)

                def _submit_mod_search(search_input: ModWebValueContainer) -> None:
                    nonlocal current_search_query
                    current_search_query = _value_as_text(search_input)
                    self._replace_browser_search_query(ui=ui, search_query=current_search_query)
                    _mod_download_rows.refresh(current_search_query)

                def _sort_mod_rows(event: ModWebValueContainer) -> None:
                    nonlocal current_sort_order
                    current_sort_order = ModWebModSortOrder(_value_as_text(event))
                    self._replace_browser_mod_sort_order(ui=ui, order=current_sort_order)
                    _mod_download_rows.refresh(current_search_query)

                toolbar_bindings: _ModWebModToolbarBindings = self._render_mod_toolbar(
                    ui=ui,
                    model=model,
                    user=user,
                    toggle_selection=toggle_selection,
                    download_selected=download_selected if capabilities.supports_raw_download else None,
                    open_client_pack=open_client_pack_dialog if supports_client_pack else None,
                    open_modlist=modlist_dialog.open,
                    open_client_pack_config=(
                        open_client_pack_configuration
                        if supports_client_pack and self._user_has_level(user, Power_Level.admin)
                        else None
                    ),
                    find_metadata=(
                        (lambda: start_metadata_operation(find_bulk_mod_metadata))
                        if is_minecraft_app
                        and self._user_has_level(
                            user,
                            required_mod_mutation_level(NodeModMutationAction.UPDATE_PROPERTIES),
                        )
                        else None
                    ),
                    check_all_updates=(
                        check_all_mod_updates
                        if model.app_scope == config.AppScopes.factorio.value
                        and bool(model.mods.mods)
                        and self._user_has_level(user, Power_Level.user)
                        else None
                    ),
                    cancel_metadata=cancel_metadata_operation,
                    delete_selected=delete_dialog.open,
                    upload_mod=upload_picker_action,
                    add_mod_link=open_mod_link_dialog if can_install_factorio_mod_link else None,
                    show_search=show_search,
                    search_query=current_search_query,
                    on_search=_submit_mod_search if show_search else None,
                    show_sort=show_sort,
                    sort_order=current_sort_order,
                    on_sort=_sort_mod_rows if show_sort else None,
                )
                selection_button: Button | None = toolbar_bindings.selection_button
                download_button: Button | None = toolbar_bindings.download_button
                delete_control = toolbar_bindings.delete_control
                result_count_label = toolbar_bindings.result_count_label
                metadata_status_button = toolbar_bindings.metadata_status_button
                update_count()

                if can_upload_mod:
                    self._ensure_mod_list_dropzone_style(ui=ui)
                    inline_upload_control = ui.upload(
                        label="",
                        auto_upload=True,
                        multiple=True,
                    ).classes("mod-file-upload-zone mod-mod-list-dropzone w-full")
                    inline_upload_control.props["batch"] = True
                    inline_upload_control.props["field-name"] = "upload"
                    inline_upload_control.on("start", direct_upload_started, args=[])
                    inline_upload_control.on("uploaded", direct_upload_succeeded, args=[])
                    inline_upload_control.on("failed", direct_upload_failed, args=[])
                    inline_upload_control.on("rejected", direct_upload_rejected, args=[])
                    refresh_direct_upload_target()
                    direct_upload_token_timer: Timer = ui.timer(
                        _DIRECT_UPLOAD_TOKEN_REFRESH_SECONDS,
                        refresh_direct_upload_target,
                    )
                    self._register_timer_cleanup(ui=ui, timer=direct_upload_token_timer)
                    self._register_client_cleanup(ui=ui, cleanup=interrupt_direct_upload_transfer)
                    with inline_upload_control.add_slot("list"):
                        with ui.element("div").classes("mod-mod-list-drop-shell w-full"):
                            _mod_download_rows(current_search_query)
                            with ui.element("div").classes("mod-mod-list-drop-overlay"):
                                ui.label("Drop mod files to upload").classes("text-sm")
                else:
                    _mod_download_rows(current_search_query)
        return

    @staticmethod
    def _restore_virtual_mod_scroll_position(
        *,
        ui: ModWebUi,
        node_name: str,
        app_name: str,
    ) -> None:
        storage_key: str = f"mod-web:mods-scroll:{node_name}:{app_name}"
        encoded_storage_key: str = json.dumps(storage_key)
        ui.run_javascript(
            f"""
            (() => {{
              const storageKey = {encoded_storage_key};
              const selector = '.mod-virtual-mod-table .q-table__middle';
              const readPosition = () => {{
                try {{
                  const stored = window.sessionStorage.getItem(storageKey);
                  const position = stored === null ? 0 : Number(stored);
                  return Number.isFinite(position) && position >= 0 ? position : 0;
                }} catch (_error) {{
                  return 0;
                }}
              }};
              const writePosition = (position) => {{
                try {{
                  window.sessionStorage.setItem(storageKey, String(position));
                }} catch (_error) {{
                  // Storage can be unavailable in privacy-restricted browser contexts.
                }}
              }};
              const restore = () => {{
                const scroller = document.querySelector(selector);
                if (!(scroller instanceof HTMLElement)) return false;
                if (scroller.dataset.modScrollStorageKey !== storageKey) {{
                  scroller.dataset.modScrollStorageKey = storageKey;
                  scroller.addEventListener(
                    'scroll',
                    () => writePosition(scroller.scrollTop),
                    {{ passive: true }},
                  );
                }}
                scroller.scrollTop = readPosition();
                return true;
              }};
              if (!restore()) requestAnimationFrame(restore);
              setTimeout(restore, 120);
            }})();
            """
        )

    def _render_app_properties_section(
        self,
        *,
        ui: ModWebUi,
        model: ModWebBasePageModel,
        user: ModWebUser,
        tab: ModWebAppTabDefinition,
    ) -> Callable[[ModWebBasePageModel], None] | None:
        del tab
        can_manage_app_state: bool = self._user_has_level(
            user, required_app_mutation_level(NodeAppMutationAction.ENABLE)
        )
        can_edit_app_details: bool = self._user_has_level(
            user, required_app_mutation_level(NodeAppMutationAction.UPDATE_DETAILS)
        )
        if not can_manage_app_state and not can_edit_app_details:
            self._render_flat_tab_empty_state(
                ui=ui,
                title="Properties",
                description="App properties are unavailable for this user.",
            )
            return None

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
        factorio_chat_relay_use_shout_checkbox: Checkbox | None = None
        rcon_requires_online_players_checkbox: Checkbox | None = None
        save_properties_button: Button | None = None
        instance_state_button: Button | None = None
        activity_provider_checkboxes: list[tuple[str, Checkbox]] = []
        current_runtime_model: ModWebBasePageModel = model
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
            return None if parsed == 0 else parsed

        def _sync_steam_update_controls() -> None:
            if steam_update_enabled_checkbox is None or steam_update_branch_select is None:
                return
            if bool(_value_as_object(steam_update_enabled_checkbox)):
                steam_update_branch_select.enable()
                return
            steam_update_branch_select.disable()

        def _properties_toggle(
            *,
            label: str,
            value: bool,
            on_change: Callable[[object], object] | None = None,
        ) -> Checkbox:
            return ui.checkbox(label, value=value, on_change=on_change).props("dense").classes("mod-app-details-toggle")

        async def _submit_properties() -> None:
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
                raise RuntimeError("App property controls were not rendered.")
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
            except (TypeError, ValueError) as xcp:
                ui.notify(str(xcp) if str(xcp) else "Title font is invalid.", type="negative")
                return
            next_steam_update_enabled: bool | None = None
            next_steam_update_selected_branch: str | None = None
            if steam_update_preset is not None:
                if steam_update_enabled_checkbox is None or steam_update_branch_select is None:
                    raise RuntimeError("Steam update controls were not rendered.")
                next_steam_update_enabled = bool(_value_as_object(steam_update_enabled_checkbox))
                if next_steam_update_enabled:
                    next_steam_update_selected_branch = _value_as_text(steam_update_branch_select).strip()
                    if next_steam_update_selected_branch not in steam_update_branch_options:
                        ui.notify("Steam update branch is invalid.", type="negative")
                        return
            disabled_activity_provider_ids = tuple(
                provider_id
                for provider_id, checkbox in activity_provider_checkboxes
                if not bool(_value_as_object(checkbox))
            )
            try:
                result = await self._mutate_app(
                    model=current_runtime_model,
                    action=NodeAppMutationAction.UPDATE_DETAILS,
                    user=user,
                    friendly_name=next_friendly_name,
                    title_font_preset=next_title_font_preset,
                    notes=_value_as_text(notes_input),
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
                    factorio_chat_relay_use_shout=(
                        None
                        if factorio_chat_relay_use_shout_checkbox is None
                        else bool(_value_as_object(factorio_chat_relay_use_shout_checkbox))
                    ),
                    rcon_requires_online_players=(
                        None
                        if rcon_requires_online_players_checkbox is None
                        else bool(_value_as_object(rcon_requires_online_players_checkbox))
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
                    "App properties update failed: node=%s app=%s error=%s",
                    current_runtime_model.node_name,
                    current_runtime_model.app_name,
                    xcp,
                )
                ui.notify(f"App properties update failed: {xcp}", type="negative")
                return
            ui.notify(result.message, type="positive")
            self._guarded_reload(ui=ui)

        async def _handle_property_save(_: object | None = None) -> None:
            if save_properties_button is None:
                raise RuntimeError("App properties Save button was not rendered.")
            await self._run_with_loading_button(button=save_properties_button, action=_submit_properties)

        async def _change_instance_state(_: object | None = None) -> None:
            if instance_state_button is None:
                raise RuntimeError("App properties state button was not rendered.")
            action = self._app_enable_disable_action(current_runtime_model)

            async def _mutate_state() -> None:
                try:
                    result = await self._mutate_app(model=current_runtime_model, action=action, user=user)
                except Exception as xcp:
                    log.warning(
                        "App properties state update failed: node=%s app=%s action=%s error=%s",
                        current_runtime_model.node_name,
                        current_runtime_model.app_name,
                        action.value,
                        xcp,
                    )
                    ui.notify(f"App state update failed: {xcp}", type="negative")
                    return
                ui.notify(result.message, type="positive")
                self._guarded_reload(ui=ui)

            await self._run_with_loading_button(button=instance_state_button, action=_mutate_state)

        with ui.card().classes(f"{self._flat_tab_card_classes()} mod-app-properties-card"):
            with ui.column().classes("w-full gap-4 mod-app-details-layout"):
                with ui.column().classes("gap-1"):
                    ui.label("Properties").classes("text-xl font-black mod-title-small")
                    ui.label("Update instance-level details shown across web and relay surfaces.").classes(
                        "mod-subtitle text-sm"
                    )
                if can_edit_app_details:
                    resource_points = model.resource_points
                    running_cpu_points_value = "0" if resource_points is None else str(resource_points.cpu_points_running)
                    running_ram_points_value = "0" if resource_points is None else str(resource_points.ram_points_running)
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
                                ui.label("Enable or repair the default Steam updater block for this instance.").classes(
                                    "mod-subtitle text-xs"
                                )
                                steam_update_enabled_checkbox = _properties_toggle(
                                    label="Enable Steam updates",
                                    value=model.update_info is not None,
                                    on_change=lambda _: _sync_steam_update_controls(),
                                )
                                if steam_update_app_id is not None:
                                    ui.label(f"Steam App ID: {steam_update_app_id}").classes("mod-subtitle text-xs")
                                steam_update_branch_select = (
                                    ui.select(
                                        steam_update_branch_options,
                                        value=steam_update_selected_branch,
                                        label="Configured target branch",
                                    )
                                    .props("filled square dense hide-bottom-space color=accent options-dark")
                                    .classes("mod-app-details-field")
                                )
                                _sync_steam_update_controls()
                        with ui.column().classes("mod-app-details-subsection"):
                            ui.label("Resource Points").classes("mod-stat-label")
                            ui.label(
                                "Leave a startup field blank, or set it to 0, to use that resource's running points."
                            ).classes("mod-subtitle text-xs")
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
                            lifecycle_started_checkbox = _properties_toggle(
                                label="Started", value=model.lifecycle_notice_started
                            )
                            lifecycle_stopped_checkbox = _properties_toggle(
                                label="Stopped", value=model.lifecycle_notice_stopped
                            )
                            lifecycle_crashed_checkbox = _properties_toggle(
                                label="Crash", value=model.lifecycle_notice_crashed
                            )
                            if model.relay_notice_player_session is not None:
                                relay_notice_player_session_checkbox = _properties_toggle(
                                    label="Player Join/Leave", value=model.relay_notice_player_session
                                )
                            if model.relay_notice_player_death is not None:
                                relay_notice_player_death_checkbox = _properties_toggle(
                                    label="Death", value=model.relay_notice_player_death
                                )
                            if model.relay_notice_progress is not None:
                                relay_notice_progress_checkbox = _properties_toggle(
                                    label=model.relay_notice_progress_label or "Progress",
                                    value=model.relay_notice_progress,
                                )
                            if model.relay_advancements_enabled is not None:
                                relay_advancements_checkbox = _properties_toggle(
                                    label=model.relay_advancement_term or "Advancement",
                                    value=model.relay_advancements_enabled,
                                )
                            if model.factorio_chat_relay_use_shout is not None:
                                factorio_chat_relay_use_shout_checkbox = _properties_toggle(
                                    label="Chat via /say or /shout",
                                    value=model.factorio_chat_relay_use_shout,
                                )
                            if model.rcon_requires_online_players is not None:
                                rcon_requires_online_players_checkbox = _properties_toggle(
                                    label="Gate RCON commands behind online players",
                                    value=model.rcon_requires_online_players,
                                )
                        if model.activity_providers:
                            with ui.column().classes("mod-app-details-subsection"):
                                ui.label("Activity Providers").classes("mod-stat-label")
                                for provider in model.activity_providers:
                                    checkbox = _properties_toggle(label=provider.label, value=provider.enabled)
                                    activity_provider_checkboxes.append((provider.provider_id, checkbox))
                if can_manage_app_state:
                    with ui.column().classes("mod-app-details-section"):
                        ui.label("Instance State").classes("mod-stat-label")
                        instance_state_button = ui.button(
                            self._app_enable_disable_label(model),
                            on_click=_change_instance_state,
                        ).classes(f"{self._app_enable_disable_button_classes(model)} mod-app-details-state-button")
                if can_edit_app_details:
                    with ui.row().classes("w-full justify-end gap-2 mod-app-details-actions"):
                        save_properties_button = ui.button("Save", on_click=_handle_property_save).classes(
                            "mod-list-button"
                        )

        if instance_state_button is None:
            return None

        def apply_runtime_model(runtime_model: ModWebBasePageModel) -> None:
            nonlocal current_runtime_model
            current_runtime_model = runtime_model
            instance_state_button.set_text(self._app_enable_disable_label(runtime_model))
            instance_state_button.classes(
                replace=f"{self._app_enable_disable_button_classes(runtime_model)} mod-app-details-state-button"
            )

        return apply_runtime_model

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
        if not can_control_app_runtime and not can_kill_app_runtime:
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
                next_start_stop_state = self._start_stop_control_state(runtime_model)
                if force or next_start_stop_state != start_stop_control_state:
                    start_stop_button.set_text(next_start_stop_state.label)
                    start_stop_button.classes(replace=next_start_stop_state.button_classes)
                    _set_button_disabled(button=start_stop_button, disabled=next_start_stop_state.disabled)
                    start_stop_control_state = next_start_stop_state
            if can_kill_app_runtime and kill_button is not None:
                next_kill_state = self._kill_control_state(runtime_model)
                if force or next_kill_state != kill_control_state:
                    kill_button.set_text(next_kill_state.label)
                    kill_button.classes(replace="mod-list-button danger mod-toolbar-button")
                    _set_button_disabled(button=kill_button, disabled=next_kill_state.disabled)
                    kill_control_state = next_kill_state

        async def run_app_action(action: NodeAppMutationAction) -> None:
            pending_label = self._app_action_pending_label(action)
            pending_message = self._app_action_pending_message(action, model.app_friendly)
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
                result = await self._mutate_app(model=current_runtime_model, action=action, user=user)
            except Exception as xcp:
                log.warning(
                    "App mutation failed: node=%s app=%s action=%s error=%s",
                    current_runtime_model.node_name,
                    current_runtime_model.app_name,
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
            completion_message = self._app_action_completion_message(
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
            if refresh_async_runtime_model is not None:
                try:
                    _apply_runtime_control_model(await refresh_async_runtime_model(), force=True)
                except Exception as xcp:
                    log.warning(
                        "App runtime refresh failed after action: node=%s app=%s action=%s error=%s",
                        current_runtime_model.node_name,
                        current_runtime_model.app_name,
                        action.value,
                        xcp,
                    )
                return
            self._guarded_reload(ui=ui)

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
        if poll_runtime_model and refresh_async_runtime_model is not None:
            refresh_runtime_control: AsyncRefresh = self._build_async_refreshable_updater(
                refresh_async_value=refresh_async_runtime_model,
                apply_value=lambda runtime_model: _apply_runtime_control_model(runtime_model, force=False),
                error_context="Mod web app runtime control",
            )
            refresh_runtime_control_timer: Timer = ui.timer(
                _APP_RUNTIME_REFRESH_INTERVAL_SECONDS,
                lambda: asyncio.create_task(refresh_runtime_control()),
            )
            self._register_timer_cleanup(ui=ui, timer=refresh_runtime_control_timer)
        return _ModWebRuntimeToolbarBindings(
            apply_runtime_model=lambda runtime_model: _apply_runtime_control_model(runtime_model, force=False),
        )

    def _render_mod_toolbar(
        self,
        *,
        ui: ModWebUi,
        model: ModWebPageModel,
        user: ModWebUser,
        toggle_selection: Callable[[], None],
        download_selected: Callable[[], Awaitable[None]] | None,
        delete_selected: Callable[[], None],
        open_client_pack: Callable[[], None] | None = None,
        open_modlist: Callable[[], None] | None = None,
        open_client_pack_config: Callable[[], None] | None = None,
        find_metadata: Callable[[], None] | None = None,
        check_all_updates: Callable[[], Awaitable[None]] | None = None,
        cancel_metadata: Callable[[], None] | None = None,
        upload_mod: Callable[[], object] | None = None,
        add_mod_link: Callable[[], object] | None = None,
        show_search: bool = False,
        search_query: str = "",
        on_search: Callable[[ModWebValueContainer], None] | None = None,
        show_sort: bool = False,
        sort_order: ModWebModSortOrder = ModWebModSortOrder.NEWEST,
        on_sort: Callable[[ModWebValueContainer], None] | None = None,
    ) -> _ModWebModToolbarBindings:
        can_upload_mod: bool = upload_mod is not None and self._user_has_level(user, Power_Level.user)
        can_add_mod_link: bool = add_mod_link is not None and self._user_has_level(user, Power_Level.user)
        can_delete_mods: bool = self._user_has_level(user, Power_Level.sudo) and any(
            not self._is_builtin_mod(entry) for entry in model.mods.mods
        )
        show_bulk_mod_actions: bool = bool(model.mods.mods)
        if (
            not can_upload_mod
            and not can_add_mod_link
            and not show_bulk_mod_actions
            and not show_search
            and not show_sort
        ):
            return _ModWebModToolbarBindings(
                selection_button=None,
                download_button=None,
                delete_control=None,
                result_count_label=None,
                metadata_status_button=None,
            )

        selection_button: Button | None = None
        download_button: Button | None = None
        delete_control: _ModWebEnableableControl | None = None
        result_count_label: Label | None = None
        metadata_status_button: Button | None = None
        menu_supported: bool = callable(getattr(ui, "menu", None)) and callable(getattr(ui, "menu_item", None))
        mobile_secondary_class: str = " mod-toolbar-mobile-secondary" if menu_supported else ""

        with ui.row().classes("mod-tab-toolbar mod-mods-toolbar w-full"):
            with ui.row().classes("mod-mods-toolbar-filters w-full"):
                if show_search:
                    if on_search is None:
                        raise ValueError("Mod search handler is not available.")
                    search_input: Input = (
                        ui.input(placeholder="Search mods", value=search_query)
                        .props("filled square dense clearable hide-bottom-space color=accent")
                        .classes("mod-config-search mod-settings-search mod-mods-toolbar-search")
                    )

                    def _submit_search() -> None:
                        on_search(search_input)

                    def _clear_search() -> None:
                        search_input.set_value("")
                        on_search(search_input)

                    search_input.on("keydown.enter", _submit_search)
                    search_input.on("clear", _clear_search)
                if show_sort:
                    if on_sort is None:
                        raise ValueError("Mod sort handler is not available.")
                    (
                        ui.select(
                            {order.value: order.label for order in ModWebModSortOrder},
                            value=sort_order.value,
                            on_change=on_sort,
                        )
                        .props("filled square dense hide-bottom-space color=accent options-dark")
                        .classes("mod-config-select mod-mods-toolbar-sort")
                    )
                result_count_label = ui.label(
                    self._mod_result_count_label(
                        visible_count=len(model.mods.mods),
                        total_count=len(model.mods.mods),
                    )
                ).classes("mod-mods-toolbar-result-count")
            with ui.row().classes("mod-tab-toolbar-actions mod-mods-toolbar-actions"):
                if find_metadata is not None:
                    if cancel_metadata is None:
                        raise ValueError("Metadata cancellation handler is not available.")
                    metadata_status_button = (
                        ui.button("Metadata: Running", on_click=cancel_metadata)
                        .props("flat no-caps aria-live=polite")
                        .classes(
                            "mod-list-button secondary mod-toolbar-button "
                            "mod-toolbar-status-button"
                        )
                    )
                    metadata_status_button.set_visibility(False)
                if show_bulk_mod_actions:
                    selection_button = ui.button("", on_click=toggle_selection).classes(
                        "mod-list-button secondary mod-toolbar-button mod-toolbar-selection-button"
                    )
                    if download_selected is not None:
                        download_button = ui.button("", on_click=download_selected).classes(
                            "mod-list-button mod-toolbar-button mod-toolbar-button-fill mod-toolbar-primary"
                        )
                    if open_client_pack is not None:
                        ui.button("Client Pack", on_click=open_client_pack).classes(
                            f"mod-list-button secondary mod-toolbar-button mod-toolbar-button-fill{mobile_secondary_class}"
                        )
                    if open_modlist is not None:
                        ui.button("Modlist", on_click=open_modlist).classes(
                            f"mod-list-button secondary mod-toolbar-button mod-toolbar-button-fill{mobile_secondary_class}"
                        )
                if can_add_mod_link:
                    ui.button("Add Link", on_click=add_mod_link).classes(
                        f"mod-list-button secondary mod-toolbar-button mod-toolbar-button-fill{mobile_secondary_class}"
                    )
                has_menu_actions = (
                    can_upload_mod
                    or open_client_pack is not None
                    or open_modlist is not None
                    or can_add_mod_link
                    or open_client_pack_config is not None
                    or find_metadata is not None
                    or check_all_updates is not None
                    or can_delete_mods
                )
                if has_menu_actions:
                    configure_label = "Configure <!>" if model.client_pack_content_dirty else "Configure"
                    if menu_supported:
                        with (
                            ui.button("")
                            .props("icon=menu flat aria-label=Mod actions")
                            .classes("mod-list-button secondary mod-toolbar-button mod-toolbar-menu-button")
                        ):
                            with ui.menu().classes("mod-chat-entry-menu mod-toolbar-menu"):
                                if open_client_pack is not None:
                                    ui.menu_item("Client Pack", on_click=open_client_pack).classes(
                                        "mod-chat-entry-menu-item mod-toolbar-menu-item "
                                        "mod-toolbar-menu-mobile-only"
                                    )
                                if open_modlist is not None:
                                    ui.menu_item("Modlist", on_click=open_modlist).classes(
                                        "mod-chat-entry-menu-item mod-toolbar-menu-item "
                                        "mod-toolbar-menu-mobile-only"
                                    )
                                if can_add_mod_link:
                                    ui.menu_item("Add Link", on_click=add_mod_link).classes(
                                        "mod-chat-entry-menu-item mod-toolbar-menu-item "
                                        "mod-toolbar-menu-mobile-only"
                                    )
                                if can_upload_mod:
                                    ui.menu_item("Upload", on_click=upload_mod).classes(
                                        "mod-chat-entry-menu-item mod-toolbar-menu-item"
                                    )
                                if open_client_pack_config is not None:
                                    ui.menu_item(configure_label, on_click=open_client_pack_config).classes(
                                        "mod-chat-entry-menu-item mod-toolbar-menu-item"
                                    )
                                if find_metadata is not None:
                                    ui.menu_item("Find Metadata", on_click=find_metadata).classes(
                                        "mod-chat-entry-menu-item mod-toolbar-menu-item"
                                    )
                                if check_all_updates is not None:
                                    ui.menu_item("Check All", on_click=check_all_updates).classes(
                                        "mod-chat-entry-menu-item mod-toolbar-menu-item"
                                    )
                                if can_delete_mods:
                                    delete_control = cast(
                                        _ModWebEnableableControl,
                                        cast(
                                            object,
                                            ui.menu_item("Delete", on_click=delete_selected).classes(
                                                "mod-chat-entry-menu-item mod-toolbar-menu-item "
                                                "mod-toolbar-menu-item-danger"
                                            ),
                                        ),
                                    )
                    else:
                        if can_upload_mod:
                            ui.button("Upload", on_click=upload_mod).classes(
                                "mod-list-button secondary mod-toolbar-button mod-toolbar-button-fill"
                            )
                        if open_client_pack_config is not None:
                            ui.button(configure_label, on_click=open_client_pack_config).classes(
                                "mod-list-button secondary mod-toolbar-button mod-toolbar-button-fill"
                            )
                        if find_metadata is not None:
                            ui.button("Find Metadata", on_click=find_metadata).classes(
                                "mod-list-button secondary mod-toolbar-button mod-toolbar-button-fill"
                            )
                        if check_all_updates is not None:
                            ui.button("Check All", on_click=check_all_updates).classes(
                                "mod-list-button secondary mod-toolbar-button mod-toolbar-button-fill"
                            )
                        if can_delete_mods:
                            delete_control = ui.button("Delete", on_click=delete_selected).classes(
                                "mod-list-button danger mod-toolbar-button mod-toolbar-button-fill"
                            )
        return _ModWebModToolbarBindings(
            selection_button=selection_button,
            download_button=download_button,
            delete_control=delete_control,
            result_count_label=result_count_label,
            metadata_status_button=metadata_status_button,
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
    def _mods_header_badges(
        summary: NodeModSummary,
        *,
        client_pack_version: str | None = None,
    ) -> tuple[_ModWebBadgeSpec, ...]:
        mod_label = "mod" if summary.total_count == 1 else "mods"
        coremod_label = "coremod" if summary.coremod_count == 1 else "coremods"
        badges: list[_ModWebBadgeSpec] = [_ModWebBadgeSpec(text=f"{summary.total_count} {mod_label}", tone="black")]
        if client_pack_version is not None:
            badges.append(_ModWebBadgeSpec(text=f"pack {client_pack_version}", tone="grey"))
        if summary.non_downloadable_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{summary.non_downloadable_count} blocked", tone="warn"))
        if summary.downloadable_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{summary.downloadable_count} downloadable", tone="purple"))
        if summary.client_only_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{summary.client_only_count} client only", tone="purple"))
        if summary.coremod_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{summary.coremod_count} {coremod_label}", tone="red"))
        return tuple[_ModWebBadgeSpec, ...](badges)
