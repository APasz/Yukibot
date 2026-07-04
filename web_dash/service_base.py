from __future__ import annotations

from typing import TYPE_CHECKING, overload

from .backend import ModWebDashboardBackend
from .nicegui_protocols import AsyncRefresh, ModWebFastApiApp, ModWebRouteUi, WebChatRelayPublisher
from .stream_broker import (
    ConsoleStreamKey,
    RemoteAppStreamKey,
    RemoteChatStreamKey,
    RemoteNodeStreamKey,
    SharedAsyncStreamBroker,
)
from .runtime_imports import (
    AbstractEventLoop,
    Access_Control,
    App_Manager,
    Awaitable,
    BadgeTone,
    Callable,
    Checkbox,
    GatewayBot,
    Html,
    Label,
    Literal,
    ManagedApp,
    ModWebAuthService,
    ModWebUser,
    NodeApiService,
    NodeAppEntry,
    NodeAppStateStreamEvent,
    NodeAppMutationAction,
    NodeAppMutationResult,
    NodeAppRuntimeSummary,
    NodeBlueprintList,
    NodeBlueprintMutationResult,
    NodeCapacityMutationResult,
    NodeDiskManagementState,
    NodeDiskSettingsMutationResult,
    NodeConfigContent,
    NodeConfigList,
    NodeConsoleActionExecutionResult,
    NodeConsoleActionList,
    NodeConsoleStdoutSnapshot,
    NodeFontSourceSettingsMutationResult,
    NodeMinecraftRecipeMutationResult,
    NodeModEntry,
    NodeModList,
    NodeModMutationResult,
    NodeModUploadBatchResult,
    NodeSaveList,
    NodeSaveMutationResult,
    NodeSettingList,
    NodeSettingMutationResult,
    NodeSettingsActionResult,
    NodeSystemHistory,
    NodeSystemAction,
    NodeSystemActionResult,
    NodeRestartScheduleState,
    NodeSystemSummary,
    NodeStateStreamEvent,
    Path,
    Power_Level,
    RestartTarget,
    RedirectResponse,
    StarletteResponse,
    Tooltip,
    aiohttp,
    cast,
    config,
    requests,
    threading,
)
from .types import (
    ModWebAppLink,
    ModWebAppTabDefinition,
    ModWebBasePageModel,
    ModWebDirectUploadTarget,
    ModWebHomeNodeSummary,
    ModWebMinecraftItemRegistrySummary,
    ModWebMinecraftRecipeBookSummary,
    ModWebModSortOrder,
    ModWebNodeAppSection,
    ModWebNodeLink,
    ModWebNodeStatus,
    ModWebOverviewPageModel,
    ModWebPageModel,
    ModWebSearchOption,
    ModWebSevenDaysSandboxOptionsSummary,
    ModWebTitleStat,
    _ModWebBadgeSpec,
    _ModWebChatSurfaceConfig,
    RemoteChatBrokerEvent,
    _ModWebKillControlState,
    _ModWebStartStopControlState,
    _ModWebStatusPageConfig,
)
from .utils import _http_exception

if TYPE_CHECKING:
    from nicegui.element import Element


class ModWebServiceSupport:
    _backend: ModWebDashboardBackend = cast(ModWebDashboardBackend, cast(object, None))
    _startup_signal: threading.Event = cast(threading.Event, cast(object, None))
    _started: bool = False
    _routes_registered: bool = False
    _shutting_down: bool = False
    _remote_http_session: aiohttp.ClientSession | None = None
    _remote_http_session_loop: AbstractEventLoop | None = None
    _remote_sync_http_local: threading.local = cast(threading.local, cast(object, None))
    _remote_sync_http_sessions: list[requests.Session] = cast(list[requests.Session], cast(object, None))
    _remote_sync_http_sessions_lock: threading.Lock = cast(threading.Lock, cast(object, None))
    _remote_node_state_broker: SharedAsyncStreamBroker[
        RemoteNodeStreamKey, NodeStateStreamEvent
    ] = cast(SharedAsyncStreamBroker[RemoteNodeStreamKey, NodeStateStreamEvent], cast(object, None))
    _remote_app_state_broker: SharedAsyncStreamBroker[
        RemoteAppStreamKey, NodeAppStateStreamEvent
    ] = cast(SharedAsyncStreamBroker[RemoteAppStreamKey, NodeAppStateStreamEvent], cast(object, None))
    _remote_chat_broker: SharedAsyncStreamBroker[
        RemoteChatStreamKey, RemoteChatBrokerEvent
    ] = cast(SharedAsyncStreamBroker[RemoteChatStreamKey, RemoteChatBrokerEvent], cast(object, None))
    _console_stdout_broker: SharedAsyncStreamBroker[
        ConsoleStreamKey, NodeConsoleStdoutSnapshot
    ] = cast(SharedAsyncStreamBroker[ConsoleStreamKey, NodeConsoleStdoutSnapshot], cast(object, None))

    @overload
    def __getattr__(
        self, name: Literal["_remote_http_client"]
    ) -> Callable[[], Awaitable[aiohttp.ClientSession]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_aiohttp_client_timeout"]
    ) -> Callable[[float | tuple[float, float]], aiohttp.ClientTimeout]: ...

    @overload
    def __getattr__(self, name: Literal["_close_remote_http_client"]) -> Callable[[], Awaitable[None]]: ...

    @overload
    def __getattr__(self, name: Literal["_remote_sync_http_client"]) -> Callable[[], requests.Session]: ...

    @overload
    def __getattr__(self, name: Literal["_on_shutdown"]) -> Callable[[], Awaitable[None]]: ...

    @overload
    def __getattr__(self, name: Literal["_map_client_stylesheet"]) -> Callable[[], str]: ...

    @overload
    def __getattr__(self, name: Literal["_map_client_script"]) -> Callable[[], str]: ...

    @overload
    def __getattr__(self, name: Literal["_action_link"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_app_action_pending_label"]) -> Callable[..., str | None]: ...

    @overload
    def __getattr__(self, name: Literal["_app_action_pending_message"]) -> Callable[..., str | None]: ...

    @overload
    def __getattr__(self, name: Literal["_app_action_completion_message"]) -> Callable[..., str | None]: ...

    @overload
    def __getattr__(self, name: Literal["_app_enable_disable_action"]) -> Callable[..., NodeAppMutationAction]: ...

    @overload
    def __getattr__(self, name: Literal["_app_enable_disable_button_classes"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_app_enable_disable_label"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_app_footprint_value"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_app_link_from_entry"]) -> Callable[..., ModWebAppLink]: ...

    @overload
    def __getattr__(self, name: Literal["_app_link_tabs"]) -> Callable[..., tuple[ModWebAppTabDefinition, ...]]: ...

    @overload
    def __getattr__(self, name: Literal["_app_link_with_tabs"]) -> Callable[..., ModWebAppLink]: ...

    @overload
    def __getattr__(self, name: Literal["_app_list_api_actions_enabled"]) -> Callable[..., bool]: ...

    @overload
    def __getattr__(self, name: Literal["_absolute_node_api_base_url"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_app_list_view_url"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_app_page_classes"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_app_page_hero_shell_classes"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_app_runtime_state_class"]) -> Callable[..., str | None]: ...

    @overload
    def __getattr__(self, name: Literal["_app_start_blocked_local"]) -> Callable[..., bool]: ...

    @overload
    def __getattr__(self, name: Literal["_app_start_blocked_remote"]) -> Callable[..., bool]: ...

    @overload
    def __getattr__(self, name: Literal["_apply_theme"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_attach_text_tooltip"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_attach_badge_tooltip"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_attach_html_tooltip"]) -> Callable[..., tuple[Tooltip, Html]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_authorised_page_user"]
    ) -> Callable[..., Awaitable[ModWebUser | None]]: ...

    @overload
    def __getattr__(self, name: Literal["_badge"]) -> Callable[..., Label]: ...

    @overload
    def __getattr__(self, name: Literal["_badge_avatar"]) -> Callable[..., Element]: ...

    @overload
    def __getattr__(self, name: Literal["_badge_class_name"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_badge_spec"]) -> Callable[..., Element]: ...

    @overload
    def __getattr__(self, name: Literal["_badge_link"]) -> Callable[..., Element]: ...

    @overload
    def __getattr__(self, name: Literal["_build_app_title_stats"]) -> Callable[..., tuple[ModWebTitleStat, ...]]: ...

    @overload
    def __getattr__(self, name: Literal["_build_async_refreshable_updater"]) -> Callable[..., AsyncRefresh]: ...

    @overload
    def __getattr__(
        self, name: Literal["_build_framework_error_response"]
    ) -> Callable[..., Awaitable[StarletteResponse]]: ...

    @overload
    def __getattr__(self, name: Literal["_build_home_title_stats"]) -> Callable[..., tuple[ModWebTitleStat, ...]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_overview_model_from_local_page_data"]
    ) -> Callable[..., ModWebOverviewPageModel]: ...

    @overload
    def __getattr__(
        self, name: Literal["_build_overview_page_model"]
    ) -> Callable[..., Awaitable[ModWebOverviewPageModel]]: ...

    @overload
    def __getattr__(self, name: Literal["_build_page_model"]) -> Callable[..., Awaitable[ModWebPageModel]]: ...

    @overload
    def __getattr__(self, name: Literal["_page_model_from_local_page_data"]) -> Callable[..., ModWebPageModel]: ...

    @overload
    def __getattr__(self, name: Literal["_current_node_link"]) -> Callable[..., ModWebNodeLink]: ...

    @overload
    def __getattr__(self, name: Literal["_build_system_title_stats"]) -> Callable[..., tuple[ModWebTitleStat, ...]]: ...

    @overload
    def __getattr__(self, name: Literal["_build_node_system_stats"]) -> Callable[..., tuple[ModWebTitleStat, ...]]: ...

    @overload
    def __getattr__(self, name: Literal["_chat_player_count_badge"]) -> Callable[..., _ModWebBadgeSpec | None]: ...

    @overload
    def __getattr__(self, name: Literal["_chat_client_javascript"]) -> Callable[[], str]: ...

    @overload
    def __getattr__(self, name: Literal["_chat_room_app"]) -> Callable[..., object | None]: ...

    @overload
    def __getattr__(self, name: Literal["_config_card_description"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_console_action_count_badge_text"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_create_remote_app_state_subscription"]) -> Callable[..., Callable[[], None]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_create_remote_console_stdout_subscription"]
    ) -> Callable[..., Callable[[], None]]: ...

    @overload
    def __getattr__(self, name: Literal["_create_remote_node_state_subscription"]) -> Callable[..., Callable[[], None]]: ...

    @overload
    def __getattr__(self, name: Literal["_download_base_url"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_download_feedback_message"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_download_query"]) -> Callable[..., dict[str, object]]: ...

    @overload
    def __getattr__(self, name: Literal["_default_mod_web_node_name"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_download_selection_label"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_delete_selection_label"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_empty_config_list"]) -> Callable[..., NodeConfigList]: ...

    @overload
    def __getattr__(self, name: Literal["_fake_chat_select_props"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_flat_tab_card_classes"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_friendly_remote_node_error_text"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_hero_action_row_classes"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_hero_badge_row_classes"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_hero_badges_classes"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_hero_card_classes"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_hero_card_style"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_hero_header_classes"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_hero_header_main_classes"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_hero_shell_classes"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_hero_support_classes"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_hero_title_classes"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(
        self, name: Literal["_kubejs_recipe_addon_labels"]
    ) -> Callable[[tuple[str, ...]], tuple[str, ...]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_home_app_sections"]
    ) -> Callable[..., Awaitable[tuple[ModWebNodeAppSection, ...]]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_home_node_summaries"]
    ) -> Callable[..., Awaitable[tuple[ModWebHomeNodeSummary, ...]]]: ...

    @overload
    def __getattr__(self, name: Literal["_interactive_badge"]) -> Callable[..., Element]: ...

    @overload
    def __getattr__(self, name: Literal["_is_builtin_mod"]) -> Callable[..., bool]: ...

    @overload
    def __getattr__(self, name: Literal["_kill_control_state"]) -> Callable[..., _ModWebKillControlState]: ...

    @overload
    def __getattr__(
        self, name: Literal["_known_bot_snapshots"]
    ) -> Callable[..., tuple[config.BotMetadataSnapshot, ...]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_known_bot_snapshot_for_node"]
    ) -> Callable[..., config.BotMetadataSnapshot | None]: ...

    @overload
    def __getattr__(self, name: Literal["_login_node_status_badge_text"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_login_node_status_badge_tone"]) -> Callable[..., BadgeTone]: ...

    @overload
    def __getattr__(
        self, name: Literal["_login_node_statuses_async"]
    ) -> Callable[..., Awaitable[tuple[ModWebNodeStatus, ...]]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_local_chat_surface_config"]
    ) -> Callable[..., Awaitable[_ModWebChatSurfaceConfig]]: ...

    @overload
    def __getattr__(self, name: Literal["_model_with_runtime_state"]) -> Callable[..., ModWebBasePageModel]: ...

    @overload
    def __getattr__(self, name: Literal["_mutate_app"]) -> Callable[..., Awaitable[NodeAppMutationResult]]: ...

    @overload
    def __getattr__(self, name: Literal["_mutate_mod"]) -> Callable[..., Awaitable[NodeModMutationResult]]: ...

    @overload
    def __getattr__(self, name: Literal["_node_capacity"]) -> Callable[..., Awaitable[config.NodeCapacityProfile]]: ...

    @overload
    def __getattr__(self, name: Literal["_node_disk_settings"]) -> Callable[..., Awaitable[NodeDiskManagementState]]: ...

    @overload
    def __getattr__(self, name: Literal["_node_font_sources"]) -> Callable[..., Awaitable[config.NodeFontSourceSettings]]: ...

    @overload
    def __getattr__(self, name: Literal["_node_badge_style"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_node_links"]) -> Callable[..., tuple[ModWebNodeLink, ...]]: ...

    @overload
    def __getattr__(self, name: Literal["_node_role_color_hex"]) -> Callable[..., str | None]: ...

    @overload
    def __getattr__(self, name: Literal["_primary_guild_bot_role_color_hex"]) -> Callable[..., str | None]: ...

    @overload
    def __getattr__(self, name: Literal["_node_bot_user_id"]) -> Callable[..., int | None]: ...

    @overload
    def __getattr__(self, name: Literal["_mod_web_bot"]) -> Callable[..., GatewayBot | None]: ...

    @overload
    def __getattr__(self, name: Literal["_node_text_style"]) -> Callable[..., str | None]: ...

    @overload
    def __getattr__(self, name: Literal["_on_startup"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_persist_uploaded_file"]) -> Callable[..., Awaitable[Path]]: ...

    @overload
    def __getattr__(self, name: Literal["_page_tabs"]) -> Callable[..., tuple[ModWebAppTabDefinition, ...]]: ...

    @overload
    def __getattr__(self, name: Literal["_page_model_with_tabs"]) -> Callable[..., ModWebBasePageModel]: ...

    @overload
    def __getattr__(self, name: Literal["_player_count_tooltip_html"]) -> Callable[..., str | None]: ...

    @overload
    def __getattr__(self, name: Literal["_tooltip_lines_html"]) -> Callable[[tuple[str, ...]], str | None]: ...

    @overload
    def __getattr__(
        self, name: Literal["_update_node_capacity"]
    ) -> Callable[..., Awaitable[NodeCapacityMutationResult]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_update_node_disk_settings"]
    ) -> Callable[..., Awaitable[NodeDiskSettingsMutationResult]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_update_node_font_sources"]
    ) -> Callable[..., Awaitable[NodeFontSourceSettingsMutationResult]]: ...

    @overload
    def __getattr__(self, name: Literal["_refresh_runtime_model"]) -> Callable[..., Awaitable[ModWebBasePageModel]]: ...

    @overload
    def __getattr__(self, name: Literal["_register_client_cleanup"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_resolved_app_color_hex"]) -> Callable[..., str | None]: ...

    @overload
    def __getattr__(self, name: Literal["_register_routes"]) -> Callable[[ModWebFastApiApp, ModWebRouteUi], None]: ...

    @overload
    def __getattr__(self, name: Literal["_register_timer_cleanup"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(
        self, name: Literal["_append_minecraft_recipe_mutation"]
    ) -> Callable[..., Awaitable[NodeMinecraftRecipeMutationResult]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_replace_minecraft_recipe_mutation"]
    ) -> Callable[..., Awaitable[NodeMinecraftRecipeMutationResult]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_delete_minecraft_recipe_mutation"]
    ) -> Callable[..., Awaitable[NodeMinecraftRecipeMutationResult]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_subscribe_local_app_console_stdout"]
    ) -> Callable[..., Callable[[], None]]: ...

    @overload
    def __getattr__(self, name: Literal["_remote_app_entry_async"]) -> Callable[..., Awaitable[NodeAppEntry]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_app_runtime_summary_async"]
    ) -> Callable[..., Awaitable[NodeAppRuntimeSummary]]: ...

    @overload
    def __getattr__(self, name: Literal["_remote_apps_async"]) -> Callable[..., Awaitable[tuple[NodeAppEntry, ...]]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_chat_surface_config"]
    ) -> Callable[..., Awaitable[_ModWebChatSurfaceConfig]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_config_content_async"]
    ) -> Callable[..., Awaitable[NodeConfigContent]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_config_list_async"]
    ) -> Callable[..., Awaitable[NodeConfigList]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_config_write_async"]
    ) -> Callable[..., Awaitable[NodeConfigContent]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_console_action_list_async"]
    ) -> Callable[..., Awaitable[NodeConsoleActionList]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_console_stdout_async"]
    ) -> Callable[..., Awaitable[NodeConsoleStdoutSnapshot]]: ...

    @overload
    def __getattr__(self, name: Literal["_remote_download_redirect"]) -> Callable[..., RedirectResponse]: ...

    @overload
    def __getattr__(self, name: Literal["_remote_download_url"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_direct_mod_upload_target"]) -> Callable[..., ModWebDirectUploadTarget]: ...

    @overload
    def __getattr__(self, name: Literal["_direct_save_upload_target"]) -> Callable[..., ModWebDirectUploadTarget]: ...

    @overload
    def __getattr__(self, name: Literal["_start_direct_upload_transfer"]) -> Callable[..., int]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_execute_console_action_async"]
    ) -> Callable[..., Awaitable[NodeConsoleActionExecutionResult]]: ...

    @overload
    def __getattr__(self, name: Literal["_remote_json_async"]) -> Callable[..., Awaitable[dict[str, object]]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_bytes_async"]
    ) -> Callable[..., Awaitable[tuple[bytes, str | None, tuple[tuple[str, str], ...]]]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_mod_list_async"]
    ) -> Callable[..., Awaitable[NodeModList]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_minecraft_recipe_summaries_async"]
    ) -> Callable[
        ..., Awaitable[tuple[ModWebMinecraftRecipeBookSummary, ModWebMinecraftItemRegistrySummary]]
    ]: ...

    @overload
    def __getattr__(self, name: Literal["_remote_mod_uploads"]) -> Callable[..., NodeModUploadBatchResult]: ...

    @overload
    def __getattr__(self, name: Literal["_remote_node_link"]) -> Callable[..., ModWebNodeLink]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_node_system_summary_async"]
    ) -> Callable[..., Awaitable[NodeSystemSummary]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_node_system_history_async"]
    ) -> Callable[..., Awaitable[NodeSystemHistory]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_node_system_action_async"]
    ) -> Callable[[ModWebNodeLink, NodeSystemAction, bool, ModWebUser], Awaitable[NodeSystemActionResult]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_restart_schedules_async"]
    ) -> Callable[[ModWebNodeLink, ModWebUser], Awaitable[NodeRestartScheduleState]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_update_restart_schedule_async"]
    ) -> Callable[
        [ModWebNodeLink, RestartTarget, int | None, int | None, ModWebUser], Awaitable[NodeRestartScheduleState]
    ]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_skip_restart_schedule_async"]
    ) -> Callable[[ModWebNodeLink, RestartTarget, ModWebUser], Awaitable[NodeRestartScheduleState]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_node_system_summary_or_none_async"]
    ) -> Callable[..., Awaitable[NodeSystemSummary | None]]: ...

    @overload
    def __getattr__(self, name: Literal["_remote_overview_page_model"]) -> Callable[..., ModWebOverviewPageModel]: ...

    @overload
    def __getattr__(self, name: Literal["_remote_page_model"]) -> Callable[..., ModWebPageModel]: ...

    @overload
    def __getattr__(self, name: Literal["_remote_portal_redirect"]) -> Callable[..., RedirectResponse | None]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_blueprint_delete_async"]
    ) -> Callable[..., Awaitable[NodeBlueprintMutationResult]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_blueprint_list_async"]
    ) -> Callable[..., Awaitable[NodeBlueprintList]]: ...

    @overload
    def __getattr__(self, name: Literal["_remote_blueprint_upload"]) -> Callable[..., NodeBlueprintMutationResult]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_save_list_async"]
    ) -> Callable[..., Awaitable[NodeSaveList]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_save_delete_async"]
    ) -> Callable[..., Awaitable[NodeSaveMutationResult]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_save_rename_async"]
    ) -> Callable[..., Awaitable[NodeSaveMutationResult]]: ...

    @overload
    def __getattr__(self, name: Literal["_remote_save_upload"]) -> Callable[..., NodeSaveMutationResult]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_setting_list_async"]
    ) -> Callable[..., Awaitable[NodeSettingList]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_sevendays_sandbox_options_summary_async"]
    ) -> Callable[..., Awaitable[ModWebSevenDaysSandboxOptionsSummary]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_setting_write_async"]
    ) -> Callable[..., Awaitable[NodeSettingMutationResult]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_settings_reload_async"]
    ) -> Callable[..., Awaitable[NodeSettingsActionResult]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_settings_save_async"]
    ) -> Callable[..., Awaitable[NodeSettingsActionResult]]: ...

    @overload
    def __getattr__(self, name: Literal["_remote_token"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_render_app_node_badge"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_render_about_page"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_render_chat_event_group"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(
        self, name: Literal["_render_chat_endpoint_badge"]
    ) -> Callable[..., tuple[Label, Tooltip, Html]]: ...

    @overload
    def __getattr__(self, name: Literal["_render_chat_page"]) -> Callable[..., Awaitable[None]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_render_chat_section"]
    ) -> Callable[..., Callable[[ModWebBasePageModel], None]]: ...

    @overload
    def __getattr__(self, name: Literal["_render_config_editor"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(
        self, name: Literal["_render_console_editor"]
    ) -> Callable[..., Callable[[ModWebBasePageModel], None] | None]: ...

    @overload
    def __getattr__(self, name: Literal["_render_error_page"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_render_forbidden_page"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_render_flat_tab_empty_state"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(
        self, name: Literal["_render_flat_tab_header"]
    ) -> Callable[..., tuple[Label | None, Label | None]]: ...

    @overload
    def __getattr__(self, name: Literal["_render_framework_page_exception"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_render_home_page"]) -> Callable[..., Awaitable[None]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_render_live_title_stats"]
    ) -> Callable[..., Callable[[tuple[ModWebTitleStat, ...]], None]]: ...

    @overload
    def __getattr__(self, name: Literal["_render_mod_download_row"]) -> Callable[..., Checkbox | None]: ...

    @overload
    def __getattr__(
        self, name: Literal["_mod_options"]
    ) -> Callable[[tuple[NodeModEntry, ...]], tuple[ModWebSearchOption, ...]]: ...

    @overload
    def __getattr__(self, name: Literal["_mod_result_count_label"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_render_mods_page"]) -> Callable[..., Awaitable[None]]: ...

    @overload
    def __getattr__(self, name: Literal["_render_node_mods_page"]) -> Callable[..., Awaitable[None]]: ...

    @overload
    def __getattr__(self, name: Literal["_render_node_system_dashboard"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_render_node_system_page"]) -> Callable[..., Awaitable[None]]: ...

    @overload
    def __getattr__(self, name: Literal["_render_node_unavailable_card"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_render_oauth_failure_page"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_render_overview_page"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_render_page"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_render_remote_chat_page"]) -> Callable[..., Awaitable[None]]: ...

    @overload
    def __getattr__(self, name: Literal["_render_remote_node_unavailable_page"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(
        self, name: Literal["_remote_node_error_is_transient"]
    ) -> Callable[[BaseException], bool]: ...

    @overload
    def __getattr__(self, name: Literal["_render_auth_setup_page"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_render_status_page_panel"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_render_blueprints_editor"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_render_saves_editor"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_render_settings_editor"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_render_user_header"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_request_path"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_request_url_with_query_values"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_replace_browser_search_query"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_running_value"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_require_http_user"]) -> Callable[..., ModWebUser]: ...

    @overload
    def __getattr__(self, name: Literal["_resolve_exception_handler_result"]) -> Callable[..., Awaitable[object]]: ...

    @overload
    def __getattr__(self, name: Literal["_selection_toggle_label"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(
        self, name: Literal["_section_badge_rows"]
    ) -> Callable[..., tuple[tuple[_ModWebBadgeSpec, ...], ...]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_filter_mod_entries"]
    ) -> Callable[..., tuple[NodeModEntry, ...]]: ...

    @overload
    def __getattr__(
        self, name: Literal["_sort_mod_entries"]
    ) -> Callable[[tuple[NodeModEntry, ...], ModWebModSortOrder], tuple[NodeModEntry, ...]]: ...

    @overload
    def __getattr__(self, name: Literal["_set_badge_state"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_run_node_presence_badges_javascript"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_set_html_tooltip_state"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_set_chat_endpoint_badge_state"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_set_optional_badge_state"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_settings_card_description"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_should_render_framework_error_page"]) -> Callable[..., bool]: ...

    @overload
    def __getattr__(self, name: Literal["_framework_http_error_config"]) -> Callable[..., _ModWebStatusPageConfig]: ...

    @overload
    def __getattr__(self, name: Literal["_simulated_down_node_names"]) -> Callable[..., tuple[str, ...]]: ...

    @overload
    def __getattr__(self, name: Literal["_start_download"]) -> Callable[..., Awaitable[None]]: ...

    @overload
    def __getattr__(self, name: Literal["_start_stop_control_state"]) -> Callable[..., _ModWebStartStopControlState]: ...

    @overload
    def __getattr__(self, name: Literal["_subscribe_local_app_state"]) -> Callable[..., Callable[[], None]]: ...

    @overload
    def __getattr__(self, name: Literal["_system_cpu_entry"]) -> Callable[..., tuple[str, BadgeTone]]: ...

    @overload
    def __getattr__(self, name: Literal["_system_ram_entry"]) -> Callable[..., tuple[str, BadgeTone]]: ...

    @overload
    def __getattr__(self, name: Literal["_system_storage_entry"]) -> Callable[..., tuple[str, BadgeTone]]: ...

    @overload
    def __getattr__(self, name: Literal["_system_uptime_entry"]) -> Callable[..., tuple[str, BadgeTone]]: ...

    @overload
    def __getattr__(self, name: Literal["_tab_section_body_classes"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_toggle_simulated_down_node_url"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_upload_mods"]) -> Callable[..., Awaitable[NodeModUploadBatchResult]]: ...

    @overload
    def __getattr__(self, name: Literal["_warn_page_section_load_failure"]) -> Callable[..., None]: ...

    @overload
    def __getattr__(self, name: Literal["_user_avatar_markup"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_user_avatar_uri"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_user_level_label"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_user_level_tone"]) -> Callable[..., BadgeTone]: ...

    @overload
    def __getattr__(self, name: Literal["_web_chat_author_display_name"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["_web_display_name"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["app_chat_path"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["app_path"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["index_path"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["node_app_chat_path"]) -> Callable[..., str]: ...

    @overload
    def __getattr__(self, name: Literal["node_app_path"]) -> Callable[..., str]: ...

    def __getattr__(self, name: str) -> Callable[..., object]:
        raise AttributeError(name)

    @property
    def _manager(self) -> App_Manager | None:
        return self._backend.manager

    @_manager.setter
    def _manager(self, manager: App_Manager | None) -> None:
        self._backend.replace_manager(manager)

    @property
    def _acl(self) -> Access_Control | None:
        return self._backend.acl

    @_acl.setter
    def _acl(self, acl: Access_Control | None) -> None:
        self._backend.replace_acl(acl)

    @property
    def _auth(self) -> ModWebAuthService:
        return self._backend.auth

    @property
    def _node_api(self) -> NodeApiService:
        return self._backend.node_api

    @property
    def _chat_relay(self) -> WebChatRelayPublisher | None:
        return self._backend.chat_relay

    @_chat_relay.setter
    def _chat_relay(self, chat_relay: WebChatRelayPublisher | None) -> None:
        self._backend.replace_chat_relay_service(chat_relay)

    def _resolve_app(self, app_name: str) -> ManagedApp:
        return self._backend.resolve_app(app_name)

    def _managed_apps(self) -> tuple[ManagedApp, ...]:
        return self._backend.managed_apps()

    def _user_has_level(self, user: ModWebUser, required_level: Power_Level) -> bool:
        return self._backend.user_has_level(user_id=user.discord_id, required_level=required_level)

    def _require_user_level(self, *, user: ModWebUser, required_level: Power_Level) -> None:
        if not self._user_has_level(user, required_level):
            raise _http_exception(
                403,
                f"Insufficient level: {self._user_level(user).name.title()} < {required_level.name.title()}",
            )

    def _user_level(self, user: ModWebUser) -> Power_Level:
        return self._backend.user_level(user_id=user.discord_id)


__all__: tuple[str, ...] = ("ModWebServiceSupport",)
