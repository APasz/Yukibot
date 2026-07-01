from __future__ import annotations

from mod_web_theme import BadgeTone

from .nicegui_protocols import ModWebNotificationType
from .runtime_imports import (
    AppRuntimeFault,
    AppTitleFont,
    AppUpdateInfo,
    AppUpdateStatus,
    Awaitable,
    Button,
    Callable,
    ChatAuthorKind,
    ChatEndpointKind,
    ChatEvent,
    ChatReferenceKind,
    Enum,
    Label,
    Literal,
    NodeAppMutationAction,
    NodeAppActivityProviderEntry,
    NodeAppResourcePointSummary,
    NodeAppRuntimeSummary,
    NodeAppTransitionState,
    NodeBlueprintList,
    NodeChatRoomSnapshot,
    NodeConfigList,
    NodeConsoleActionList,
    NodeModList,
    NodeSaveList,
    NodeSettingList,
    NodeSystemSummary,
    Power_Level,
    TypeAlias,
    dataclass,
    field,
)

ChatMediaPreviewKind: TypeAlias = Literal["image", "video", "audio"]


@dataclass(frozen=True, slots=True)
class _SettingSecretConfig:
    style: str
    cycle_duration_seconds: float
    cycle_delay_seconds: float


@dataclass(frozen=True, slots=True)
class _ChatMediaPreview:
    kind: ChatMediaPreviewKind
    url: str
    label: str


@dataclass(frozen=True, slots=True)
class _ModWebBadgeSpec:
    text: str
    tone: BadgeTone
    icon: str | None = None
    tooltip_text: str | None = None


class ModWebNotificationTrayItemKind(Enum):
    GENERIC = "generic"
    UPLOAD = "upload"
    DOWNLOAD = "download"


class ModWebNotificationTrayItemState(Enum):
    IDLE = "idle"
    ACTIVE = "active"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ModWebDirectUploadTarget:
    url: str
    authorization_header: str

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("Direct upload target URL must not be empty.")
        if not self.authorization_header.startswith("Bearer ") or not self.authorization_header.removeprefix(
            "Bearer "
        ).strip():
            raise ValueError("Direct upload authorization must use a bearer token.")


class ModWebMinecraftRecipeOperationKind(Enum):
    ADD = "add"
    REMOVE = "remove"


@dataclass(frozen=True, slots=True)
class _ModWebNotificationTrayItem:
    kind: ModWebNotificationTrayItemKind
    state: ModWebNotificationTrayItemState
    label: str
    detail_text: str | None = None
    progress_percent: float | None = None
    node_color_hex: str | None = None
    app_color_hex: str | None = None
    blink: bool = False

    def __post_init__(self) -> None:
        label: str = self.label.strip()
        if not label:
            raise ValueError("Notification tray items require a label.")
        object.__setattr__(self, "label", label)
        if self.detail_text is not None:
            detail_text: str = self.detail_text.strip()
            object.__setattr__(self, "detail_text", detail_text or None)
        if self.node_color_hex is not None:
            node_color_hex: str = self.node_color_hex.strip()
            object.__setattr__(self, "node_color_hex", node_color_hex or None)
        if self.app_color_hex is not None:
            app_color_hex: str = self.app_color_hex.strip()
            object.__setattr__(self, "app_color_hex", app_color_hex or None)
        if self.progress_percent is None:
            return
        if not 0.0 <= self.progress_percent <= 100.0:
            raise ValueError("Notification tray progress must be between 0 and 100.")


@dataclass(frozen=True, slots=True)
class _ModWebAppCardBadgeSpec:
    text: str
    tone: BadgeTone
    tab_id: str | None = None


@dataclass(frozen=True, slots=True)
class _ModWebLinkSpec:
    label: str
    url: str
    new_tab: bool = field(default=False, kw_only=True)


@dataclass(frozen=True, slots=True)
class _ModWebLoginAdministrator:
    user_id: int
    display_name: str
    level: Power_Level
    avatar_hash: str | None


@dataclass(frozen=True, slots=True)
class _ModWebNotificationPreviewSpec:
    label: str
    message: str
    notification_type: ModWebNotificationType
    repeat_count: int = 1
    multi_line: bool = False
    close_button: bool | str = False
    timeout_milliseconds: int | None = None

    def __post_init__(self) -> None:
        if self.repeat_count < 1:
            raise ValueError("Notification preview repeat count must be positive.")
        if self.timeout_milliseconds is not None and self.timeout_milliseconds < 0:
            raise ValueError("Notification preview timeout must not be negative.")


@dataclass(frozen=True, slots=True)
class _ModWebTabActionSpec:
    label: str
    url: str
    new_tab: bool = field(default=False, kw_only=True)
    extra_classes: str = field(default="", kw_only=True)


@dataclass(frozen=True, slots=True)
class _ModWebAppHeroRuntimeDetails:
    status_text: str
    status_tone: BadgeTone
    relay_badge: _ModWebBadgeSpec
    version_badge: _ModWebBadgeSpec
    player_count_badge: _ModWebBadgeSpec | None = None


@dataclass(frozen=True, slots=True)
class _ModWebAppRuntimeState:
    running: bool
    enabled: bool
    transition_state: NodeAppTransitionState
    player_count: int | None
    player_capacity: int | None
    connected_player_names: tuple[str, ...]
    runtime_fault: AppRuntimeFault | None


@dataclass(frozen=True, slots=True)
class ModWebMinecraftRecipeEntry:
    operation: ModWebMinecraftRecipeOperationKind
    kind_label: str
    title: str
    detail: str
    recipe_id: str | None = None

    def __post_init__(self) -> None:
        kind_label = self.kind_label.strip()
        title = self.title.strip()
        detail = self.detail.strip()
        if not kind_label:
            raise ValueError("Minecraft recipe entries require a kind label.")
        if not title:
            raise ValueError("Minecraft recipe entries require a title.")
        if not detail:
            raise ValueError("Minecraft recipe entries require detail text.")
        object.__setattr__(self, "kind_label", kind_label)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "detail", detail)
        if self.recipe_id is not None:
            recipe_id = self.recipe_id.strip()
            object.__setattr__(self, "recipe_id", recipe_id or None)


@dataclass(frozen=True, slots=True)
class ModWebMinecraftRecipeBookSummary:
    data_path: str
    script_path: str
    entries: tuple[ModWebMinecraftRecipeEntry, ...] = ()
    mutation_mappings: tuple[dict[str, object], ...] = ()
    load_error: str | None = None

    def __post_init__(self) -> None:
        data_path = self.data_path.strip()
        script_path = self.script_path.strip()
        if not data_path:
            raise ValueError("Minecraft recipe book summaries require a data path.")
        if not script_path:
            raise ValueError("Minecraft recipe book summaries require a script path.")
        object.__setattr__(self, "data_path", data_path)
        object.__setattr__(self, "script_path", script_path)
        object.__setattr__(
            self,
            "mutation_mappings",
            tuple(dict(mapping) for mapping in self.mutation_mappings),
        )
        if len(self.mutation_mappings) != len(self.entries) and self.mutation_mappings:
            raise ValueError("Minecraft recipe book summaries require aligned entries and mutation mappings.")
        if self.load_error is not None:
            load_error = self.load_error.strip()
            object.__setattr__(self, "load_error", load_error or None)


@dataclass(frozen=True, slots=True)
class ModWebMinecraftItemRegistrySummary:
    data_path: str
    item_ids: tuple[str, ...] = ()
    block_item_ids: tuple[str, ...] = ()
    item_types_classified: bool = False
    file_exists: bool = False
    generated_at_epoch_ms: int | None = None
    load_error: str | None = None

    def __post_init__(self) -> None:
        data_path = self.data_path.strip()
        if not data_path:
            raise ValueError("Minecraft item registry summaries require a data path.")
        object.__setattr__(self, "data_path", data_path)
        if not isinstance(self.item_types_classified, bool):
            raise TypeError("Minecraft item registry summary item_types_classified must be a boolean.")
        if self.generated_at_epoch_ms is not None:
            if self.generated_at_epoch_ms < 0:
                raise ValueError("Minecraft item registry summary timestamps must not be negative.")
        normalised_item_ids = tuple(item_id.strip() for item_id in self.item_ids if item_id.strip())
        object.__setattr__(self, "item_ids", normalised_item_ids)
        normalised_block_item_ids = tuple(item_id.strip() for item_id in self.block_item_ids if item_id.strip())
        if set(normalised_block_item_ids) - set(normalised_item_ids):
            raise ValueError("Minecraft block item summary IDs must also exist in the item registry.")
        if normalised_block_item_ids and not self.item_types_classified:
            raise ValueError("Minecraft block item summaries require classified item type data.")
        object.__setattr__(self, "block_item_ids", normalised_block_item_ids)
        if self.load_error is not None:
            load_error = self.load_error.strip()
            object.__setattr__(self, "load_error", load_error or None)


@dataclass(frozen=True, slots=True)
class ModWebSevenDaysSandboxOptionEntry:
    section: str
    key: str
    value_index: int
    value_label: str
    default_index: int
    default_label: str

    def __post_init__(self) -> None:
        for field_name in ("section", "key", "value_label", "default_label"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"7D2D sandbox option {field_name} must be a non-empty string.")
            object.__setattr__(self, field_name, value.strip())


@dataclass(frozen=True, slots=True)
class ModWebSevenDaysSandboxOptionsSummary:
    data_path: str
    file_exists: bool
    generated_at: str | None = None
    sandbox_code: str | None = None
    app_version: str | None = None
    options: tuple[ModWebSevenDaysSandboxOptionEntry, ...] = ()
    load_error: str | None = None

    def __post_init__(self) -> None:
        data_path = self.data_path.strip()
        if not data_path:
            raise ValueError("7D2D sandbox options summaries require a data path.")
        object.__setattr__(self, "data_path", data_path)
        object.__setattr__(self, "generated_at", self.generated_at.strip() if self.generated_at else None)
        object.__setattr__(self, "sandbox_code", self.sandbox_code.strip() if self.sandbox_code else None)
        object.__setattr__(self, "app_version", self.app_version.strip() if self.app_version else None)
        object.__setattr__(self, "load_error", self.load_error.strip() if self.load_error else None)


@dataclass(frozen=True, slots=True)
class _ModWebStartStopControlState:
    label: str
    button_classes: str
    disabled: bool
    action: NodeAppMutationAction | None


@dataclass(frozen=True, slots=True)
class _ModWebKillControlState:
    label: str
    disabled: bool


@dataclass(frozen=True, slots=True)
class _ModWebRuntimeToolbarBindings:
    apply_runtime_model: Callable[["ModWebBasePageModel"], None] | None = None


@dataclass(frozen=True, slots=True)
class _ModWebAppHeroCornerBindings:
    apply_node_summary: Callable[[NodeSystemSummary | None], None]
    apply_app_stats: Callable[[NodeAppRuntimeSummary | None], None]


@dataclass(frozen=True, slots=True)
class _ModWebModToolbarBindings:
    selection_button: Button | None
    download_button: Button | None
    delete_button: Button | None
    result_count_label: Label | None


@dataclass(frozen=True, slots=True)
class _ModWebStatusPageConfig:
    title: str
    support_text: str
    badge_text: str
    badge_tone: BadgeTone
    accent_color_hex: str
    icon_markup: str | None = None
    detail_figure_markup: str | None = None
    detail_text: str | None = None
    detail_label: str | None = None
    context_label: str | None = None
    actions: tuple[_ModWebLinkSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class _ModWebChatPanelConfig:
    initial_snapshot: NodeChatRoomSnapshot
    refresh_snapshot: Callable[[], Awaitable[NodeChatRoomSnapshot]]
    send_message: Callable[["_ModWebChatComposeRequest"], Awaitable[ChatEvent]] | None
    subscribe_updates: Callable[[Callable[["_ModWebChatPanelSignal"], None]], Callable[[], None]] | None = None


@dataclass(frozen=True, slots=True)
class _ModWebChatSurfaceConfig:
    panel: _ModWebChatPanelConfig
    node_name: str
    app_friendly: str
    app_color_hex: str | None
    app_stats: NodeAppRuntimeSummary | None
    hero_badges: tuple[_ModWebBadgeSpec, ...] = ()
    refresh_app_stats: Callable[[], Awaitable[NodeAppRuntimeSummary | None]] | None = None
    popout_url: str | None = None
    map_url: str | None = None


@dataclass(frozen=True, slots=True)
class _ModWebChatPanelSignal:
    chat_changed: bool = False
    runtime_changed: bool = False
    snapshot: NodeChatRoomSnapshot | None = None
    app_stats: NodeAppRuntimeSummary | None = None
    events: tuple[ChatEvent, ...] = ()

    def __post_init__(self) -> None:
        if not self.chat_changed and not self.runtime_changed:
            raise ValueError("Chat panel signal must refresh chat or runtime state.")
        if self.snapshot is not None and not self.chat_changed:
            raise ValueError("Chat panel signal snapshot requires chat refresh.")
        if self.events and not self.chat_changed:
            raise ValueError("Chat panel signal deltas require chat refresh.")
        if self.app_stats is not None and not self.runtime_changed:
            raise ValueError("Chat panel signal runtime payload requires runtime refresh.")

    @classmethod
    def chat(
        cls,
        *,
        snapshot: NodeChatRoomSnapshot | None = None,
        events: tuple[ChatEvent, ...] = (),
    ) -> "_ModWebChatPanelSignal":
        return cls(chat_changed=True, snapshot=snapshot, events=events)

    @classmethod
    def runtime(cls, *, app_stats: NodeAppRuntimeSummary | None = None) -> "_ModWebChatPanelSignal":
        return cls(runtime_changed=True, app_stats=app_stats)

    @classmethod
    def both(
        cls,
        *,
        snapshot: NodeChatRoomSnapshot | None = None,
        app_stats: NodeAppRuntimeSummary | None = None,
        events: tuple[ChatEvent, ...] = (),
    ) -> "_ModWebChatPanelSignal":
        return cls(
            chat_changed=True,
            runtime_changed=True,
            snapshot=snapshot,
            app_stats=app_stats,
            events=events,
        )


@dataclass(frozen=True, slots=True)
class RemoteChatBrokerEvent:
    signal: _ModWebChatPanelSignal | None = None
    stream_healthy: bool | None = None

    def __post_init__(self) -> None:
        if self.signal is None and self.stream_healthy is None:
            raise ValueError("Remote chat broker events require a signal or health state.")


@dataclass(frozen=True, slots=True)
class _ModWebChatComposeRequest:
    content: str
    reply_to_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class _ModWebChatEventGroup:
    head_event: ChatEvent
    events: tuple[ChatEvent, ...]


class _ModWebFakeChatMessageMode(Enum):
    TEXT = "text"
    JOIN = "join"
    LEAVE = "leave"
    DEATH = "death"
    PVP_KILL = "pvp_kill"
    ADVANCEMENT = "advancement"
    GOAL = "goal"
    CHALLENGE = "challenge"
    RESEARCH = "research"
    GAME_EVENT = "game_event"
    APP_STARTED = "app_started"
    APP_STOPPED = "app_stopped"
    APP_CRASHED = "app_crashed"
    MAINTENANCE_WARNING = "maintenance_warning"
    BOT_STARTED = "bot_started"
    BOT_ERROR = "bot_error"
    EMBED = "embed"


@dataclass(slots=True)
class _ModWebFakeChatPreviewState:
    app_name: str | None = None
    source_kind: ChatEndpointKind = ChatEndpointKind.APP
    author_kind: ChatAuthorKind = ChatAuthorKind.GAME_PLAYER
    author_name: str = "Yoko"
    author_color_hex: str = ""
    author_avatar_uri: str = ""
    message_mode: _ModWebFakeChatMessageMode = _ModWebFakeChatMessageMode.TEXT
    content_text: str = "hello from preview"
    detail_text: str = "Skeleton"
    embed_title: str = "Advancement"
    embed_description: str = "Stone Age"
    source_label: str = ""
    reference_kind: ChatReferenceKind = ChatReferenceKind.NONE
    reference_author_name: str = "Taylor"
    reference_content: str = "Can you check this?"
    link_url: str = ""
    link_label: str = "preview.png"
    attachment_url: str = ""
    attachment_name: str = "preview.png"


class ModDownloadKind(Enum):
    ENABLED = "enabled"
    ALL = "all"
    SELECTED = "selected"
    SINGLE = "single"


class ModWebModSortOrder(Enum):
    NEWEST = "newest"
    OLDEST = "oldest"
    NAME_ASCENDING = "name_ascending"
    NAME_DESCENDING = "name_descending"
    SIZE_DESCENDING = "size_descending"
    SIZE_ASCENDING = "size_ascending"
    TYPE = "type"

    @property
    def label(self) -> str:
        match self:
            case ModWebModSortOrder.NEWEST:
                return "Newest first"
            case ModWebModSortOrder.OLDEST:
                return "Oldest first"
            case ModWebModSortOrder.NAME_ASCENDING:
                return "Name A–Z"
            case ModWebModSortOrder.NAME_DESCENDING:
                return "Name Z–A"
            case ModWebModSortOrder.SIZE_DESCENDING:
                return "Largest first"
            case ModWebModSortOrder.SIZE_ASCENDING:
                return "Smallest first"
            case ModWebModSortOrder.TYPE:
                return "Mod type"


@dataclass(frozen=True, slots=True)
class ModWebAppLink:
    name: str
    friendly: str
    node_name: str
    running: bool
    enabled: bool
    color_hex: str | None
    supports_mods: bool
    supports_configs: bool
    supports_saves: bool
    supports_settings: bool
    url: str
    api_url: str | None
    configs_api_url: str | None
    transition_state: NodeAppTransitionState = NodeAppTransitionState.NONE
    player_count: int | None = None
    player_capacity: int | None = None
    connected_player_names: tuple[str, ...] = ()
    runtime_fault: AppRuntimeFault | None = None
    app_scope: str | None = field(default=None, kw_only=True)
    saves_api_url: str | None = None
    settings_api_url: str | None = None
    map_url: str | None = field(default=None, kw_only=True)
    supports_blueprints: bool = field(default=False, kw_only=True)
    supports_console_actions: bool = field(default=False, kw_only=True)
    supports_chat: bool = field(default=False, kw_only=True)
    supports_updates: bool = field(default=False, kw_only=True)
    chat_url: str | None = field(default=None, kw_only=True)
    update_status: AppUpdateStatus | None = field(default=None, kw_only=True)
    runtime_changed: bool = field(default=False, kw_only=True)
    tabs: tuple["ModWebAppTabDefinition", ...] = field(default=(), kw_only=True)


@dataclass(frozen=True, slots=True)
class ModWebNodeLink:
    node_name: str
    label: str
    url: str
    api_base_url: str
    api_url: str
    is_current: bool
    latency_probe_url: str | None = field(default=None, kw_only=True)
    presence_stream_url: str | None = field(default=None, kw_only=True)


@dataclass(frozen=True, slots=True)
class ModWebNodeAppSection:
    node: ModWebNodeLink
    app_links: tuple[ModWebAppLink, ...]
    error: str | None = None
    is_simulated_down: bool = False


@dataclass(frozen=True, slots=True)
class ModWebNodeStatus:
    node: ModWebNodeLink
    alive: bool
    detail: str | None = None
    is_simulated_down: bool = False


@dataclass(frozen=True, slots=True)
class _ModWebNodePresenceBadgeSpec:
    node_name: str
    badge_element_id: int
    text_element_id: int | None
    node_label: str
    pending_text: str
    alive_text: str
    down_text: str
    presence_stream_url: str | None
    pending_class_name: str
    healthy_class_name: str
    unhealthy_class_name: str
    show_latency: bool = False

    def to_mapping(self) -> dict[str, object]:
        return {
            "node_name": self.node_name,
            "badge_element_id": self.badge_element_id,
            "text_element_id": self.text_element_id,
            "node_label": self.node_label,
            "pending_text": self.pending_text,
            "alive_text": self.alive_text,
            "down_text": self.down_text,
            "presence_stream_url": self.presence_stream_url,
            "pending_class_name": self.pending_class_name,
            "healthy_class_name": self.healthy_class_name,
            "unhealthy_class_name": self.unhealthy_class_name,
            "show_latency": self.show_latency,
        }


@dataclass(frozen=True, slots=True)
class ModWebBasePageModel:
    node_name: str
    app_name: str
    app_friendly: str
    app_color_hex: str | None
    supports_configs: bool
    config_read_level: Power_Level
    config_write_level: Power_Level
    supports_save_uploads: bool
    supports_save_rename: bool
    save_write_level: Power_Level
    configs: NodeConfigList
    saves: NodeSaveList | None
    app_stats: NodeAppRuntimeSummary | None
    app_start_blocked: bool
    settings: NodeSettingList | None
    app_title_font_preset: str = field(default=AppTitleFont.AUTO.value, kw_only=True)
    console_actions: NodeConsoleActionList | None = field(default=None, kw_only=True)
    blueprints: NodeBlueprintList | None = field(default=None, kw_only=True)
    map_url: str | None = field(default=None, kw_only=True)
    map_api_url: str | None = field(default=None, kw_only=True)
    can_write_map_annotations: bool = field(default=False, kw_only=True)
    supports_chat: bool = field(default=False, kw_only=True)
    supports_updates: bool = field(default=False, kw_only=True)
    chat_url: str | None = field(default=None, kw_only=True)
    update_info: AppUpdateInfo | None = field(default=None, kw_only=True)
    update_status: AppUpdateStatus | None = field(default=None, kw_only=True)
    resource_points: NodeAppResourcePointSummary | None = field(default=None, kw_only=True)
    app_notes: str | None = field(default=None, kw_only=True)
    join_address: str | None = field(default=None, kw_only=True)
    join_direct_ip_address: str | None = field(default=None, kw_only=True)
    lifecycle_notice_started: bool = field(default=True, kw_only=True)
    lifecycle_notice_stopped: bool = field(default=True, kw_only=True)
    lifecycle_notice_crashed: bool = field(default=True, kw_only=True)
    relay_notice_player_session: bool | None = field(default=None, kw_only=True)
    relay_notice_player_death: bool | None = field(default=None, kw_only=True)
    relay_notice_progress: bool | None = field(default=None, kw_only=True)
    relay_notice_progress_label: str | None = field(default=None, kw_only=True)
    relay_advancements_enabled: bool | None = field(default=None, kw_only=True)
    relay_advancement_term: str | None = field(default=None, kw_only=True)
    activity_providers: tuple[NodeAppActivityProviderEntry, ...] = field(default=(), kw_only=True)
    load_warnings: tuple["ModWebPageLoadWarning", ...] = field(default=(), kw_only=True)
    app_scope: str | None = field(default=None, kw_only=True)
    minecraft_recipes: ModWebMinecraftRecipeBookSummary | None = field(default=None, kw_only=True)
    minecraft_item_registry: ModWebMinecraftItemRegistrySummary | None = field(default=None, kw_only=True)
    minecraft_item_icon_api_url: str | None = field(default=None, kw_only=True)
    sevendays_sandbox_options: ModWebSevenDaysSandboxOptionsSummary | None = field(default=None, kw_only=True)
    tabs: tuple["ModWebAppTabDefinition", ...] = field(default=(), kw_only=True)


@dataclass(frozen=True, slots=True)
class ModWebPageModel(ModWebBasePageModel):
    mods: NodeModList
    download_all_url: str
    download_enabled_url: str
    mod_download_urls: dict[str, str]


@dataclass(frozen=True, slots=True)
class ModWebOverviewPageModel(ModWebBasePageModel):
    pass


@dataclass(frozen=True, slots=True)
class ModWebPageLoadWarning:
    title: str
    detail: str


@dataclass(frozen=True, slots=True)
class ModWebTitleStat:
    label: str
    value: str
    tone: BadgeTone = "grey"
    lines: tuple["ModWebTitleStatLine", ...] = ()
    show_label: bool = True


@dataclass(frozen=True, slots=True)
class ModWebTitleStatLine:
    label: str | None
    value: str
    is_section: bool = False
    tone: BadgeTone | None = None

    def __post_init__(self) -> None:
        if self.is_section and self.label is None:
            raise ValueError("Stat section lines require a label.")


@dataclass(frozen=True, slots=True)
class ModWebSearchOption:
    option_id: str
    label: str
    search_text: str


class ModWebAppSectionKind(Enum):
    UPDATE = "update"
    MODS = "mods"
    CONFIGS = "configs"
    SETTINGS = "settings"
    SAVES = "saves"
    CONSOLE = "console"
    CHAT = "chat"

    @property
    def label(self) -> str:
        if self is ModWebAppSectionKind.UPDATE:
            return "Update"
        if self is ModWebAppSectionKind.MODS:
            return "Mods"
        if self is ModWebAppSectionKind.CONFIGS:
            return "Configs"
        if self is ModWebAppSectionKind.SETTINGS:
            return "Settings"
        if self is ModWebAppSectionKind.SAVES:
            return "Saves"
        if self is ModWebAppSectionKind.CHAT:
            return "Chat"
        return "Console"

    @classmethod
    def parse(cls, raw_value: str) -> "ModWebAppSectionKind":
        section_name: str = raw_value.strip().casefold()
        if not section_name:
            raise ValueError("App section value is required.")
        for section_kind in cls:
            if section_kind.value == section_name:
                return section_kind
        raise ValueError(f"Unsupported app section kind: {raw_value}")


@dataclass(frozen=True, slots=True)
class ModWebAppTabSettingSnapshot:
    key: str
    value_text: str


@dataclass(frozen=True, slots=True)
class ModWebAppTabContext:
    app_name: str
    app_friendly: str
    app_version: str | None = None
    app_scope: str | None = None
    mod_names: tuple[str, ...] = ()
    enabled_mod_names: tuple[str, ...] = ()
    settings: tuple[ModWebAppTabSettingSnapshot, ...] = ()
    supports_map: bool = False
    supports_blueprints: bool = False
    supports_sevendays_sandbox_options: bool = False

    def has_mod(self, mod_name: str) -> bool:
        target_name: str = mod_name.strip().casefold()
        return any(candidate.casefold() == target_name for candidate in self.mod_names)

    def has_enabled_mod(self, mod_name: str) -> bool:
        target_name: str = mod_name.strip().casefold()
        return any(candidate.casefold() == target_name for candidate in self.enabled_mod_names)

    def setting_value(self, setting_key: str) -> str | None:
        target_key: str = setting_key.strip().casefold()
        for setting in self.settings:
            if setting.key.casefold() == target_key:
                return setting.value_text
        return None


class ModWebAppTabVisibilityKind(Enum):
    ALWAYS = "always"
    MIN_APP_VERSION = "min_app_version"
    HAS_MOD = "has_mod"
    SETTING_ENABLED = "setting_enabled"
    SETTING_EQUALS = "setting_equals"
    ALL = "all"
    ANY = "any"


@dataclass(frozen=True, slots=True)
class ModWebAppTabVisibilityRule:
    kind: ModWebAppTabVisibilityKind
    app_version: str | None = None
    mod_name: str | None = None
    setting_key: str | None = None
    setting_value: str | None = None
    children: tuple["ModWebAppTabVisibilityRule", ...] = ()

    def __post_init__(self) -> None:
        if self.kind is ModWebAppTabVisibilityKind.ALWAYS:
            return
        if self.kind is ModWebAppTabVisibilityKind.MIN_APP_VERSION:
            if self.app_version is None or not self.app_version.strip():
                raise ValueError("App tab minimum-version rule requires a version.")
            return
        if self.kind is ModWebAppTabVisibilityKind.HAS_MOD:
            if self.mod_name is None or not self.mod_name.strip():
                raise ValueError("App tab mod rule requires a mod name.")
            return
        if self.kind is ModWebAppTabVisibilityKind.SETTING_ENABLED:
            if self.setting_key is None or not self.setting_key.strip():
                raise ValueError("App tab setting-enabled rule requires a setting key.")
            return
        if self.kind is ModWebAppTabVisibilityKind.SETTING_EQUALS:
            if self.setting_key is None or not self.setting_key.strip():
                raise ValueError("App tab setting-equals rule requires a setting key.")
            if self.setting_value is None:
                raise ValueError("App tab setting-equals rule requires an expected value.")
            return
        if not self.children:
            raise ValueError("Composite app tab visibility rules require at least one child rule.")

    @classmethod
    def always(cls) -> "ModWebAppTabVisibilityRule":
        return cls(ModWebAppTabVisibilityKind.ALWAYS)

    @classmethod
    def min_app_version(cls, app_version: str) -> "ModWebAppTabVisibilityRule":
        return cls(ModWebAppTabVisibilityKind.MIN_APP_VERSION, app_version=app_version)

    @classmethod
    def has_mod(cls, mod_name: str) -> "ModWebAppTabVisibilityRule":
        return cls(ModWebAppTabVisibilityKind.HAS_MOD, mod_name=mod_name)

    @classmethod
    def setting_enabled(cls, setting_key: str) -> "ModWebAppTabVisibilityRule":
        return cls(ModWebAppTabVisibilityKind.SETTING_ENABLED, setting_key=setting_key)

    @classmethod
    def setting_equals(cls, setting_key: str, setting_value: str) -> "ModWebAppTabVisibilityRule":
        return cls(
            ModWebAppTabVisibilityKind.SETTING_EQUALS,
            setting_key=setting_key,
            setting_value=setting_value,
        )

    @classmethod
    def all_of(cls, *children: "ModWebAppTabVisibilityRule") -> "ModWebAppTabVisibilityRule":
        return cls(ModWebAppTabVisibilityKind.ALL, children=children)

    @classmethod
    def any_of(cls, *children: "ModWebAppTabVisibilityRule") -> "ModWebAppTabVisibilityRule":
        return cls(ModWebAppTabVisibilityKind.ANY, children=children)


@dataclass(frozen=True, slots=True)
class ModWebAppTabDefinition:
    tab_id: str
    label: str
    page_order: int
    app_card_order: int
    app_card_tone: BadgeTone
    visibility_rule: ModWebAppTabVisibilityRule = field(default_factory=ModWebAppTabVisibilityRule.always)
    show_on_app_card: bool = True
    builtin_kind: ModWebAppSectionKind | None = None
    render_handler_name: str | None = None
    badge_handler_name: str | None = None
    app_card_badge_handler_name: str | None = None
    action_handler_name: str | None = None

    def __post_init__(self) -> None:
        tab_id: str = self.tab_id.strip()
        label: str = self.label.strip()
        if not tab_id:
            raise ValueError("App tab definitions require a tab id.")
        if not label:
            raise ValueError("App tab definitions require a label.")
        if self.page_order < 0 or self.app_card_order < 0:
            raise ValueError("App tab definition orders must be non-negative.")
        if (self.builtin_kind is None) == (self.render_handler_name is None):
            raise ValueError("App tab definitions require exactly one render source.")
        if self.render_handler_name is not None and not self.render_handler_name.strip():
            raise ValueError("App tab definitions require a non-empty render handler name.")
        if self.badge_handler_name is not None and not self.badge_handler_name.strip():
            raise ValueError("App tab badge handler names must be non-empty when provided.")
        if self.app_card_badge_handler_name is not None and not self.app_card_badge_handler_name.strip():
            raise ValueError("App tab app-card badge handler names must be non-empty when provided.")
        if self.action_handler_name is not None and not self.action_handler_name.strip():
            raise ValueError("App tab action handler names must be non-empty when provided.")
        object.__setattr__(self, "tab_id", tab_id)
        object.__setattr__(self, "label", label)
        if self.render_handler_name is not None:
            object.__setattr__(self, "render_handler_name", self.render_handler_name.strip())
        if self.badge_handler_name is not None:
            object.__setattr__(self, "badge_handler_name", self.badge_handler_name.strip())
        if self.app_card_badge_handler_name is not None:
            object.__setattr__(self, "app_card_badge_handler_name", self.app_card_badge_handler_name.strip())
        if self.action_handler_name is not None:
            object.__setattr__(self, "action_handler_name", self.action_handler_name.strip())

    @classmethod
    def builtin(
        cls,
        *,
        builtin_kind: ModWebAppSectionKind,
        page_order: int,
        app_card_order: int,
        app_card_tone: BadgeTone,
        visibility_rule: ModWebAppTabVisibilityRule | None = None,
        show_on_app_card: bool = True,
    ) -> "ModWebAppTabDefinition":
        return cls(
            tab_id=builtin_kind.value,
            label=builtin_kind.label,
            page_order=page_order,
            app_card_order=app_card_order,
            app_card_tone=app_card_tone,
            visibility_rule=visibility_rule or ModWebAppTabVisibilityRule.always(),
            show_on_app_card=show_on_app_card,
            builtin_kind=builtin_kind,
        )

    @classmethod
    def custom(
        cls,
        *,
        tab_id: str,
        label: str,
        page_order: int,
        app_card_order: int,
        app_card_tone: BadgeTone,
        render_handler_name: str,
        visibility_rule: ModWebAppTabVisibilityRule | None = None,
        show_on_app_card: bool = True,
        badge_handler_name: str | None = None,
        app_card_badge_handler_name: str | None = None,
        action_handler_name: str | None = None,
    ) -> "ModWebAppTabDefinition":
        return cls(
            tab_id=tab_id,
            label=label,
            page_order=page_order,
            app_card_order=app_card_order,
            app_card_tone=app_card_tone,
            visibility_rule=visibility_rule or ModWebAppTabVisibilityRule.always(),
            show_on_app_card=show_on_app_card,
            render_handler_name=render_handler_name,
            badge_handler_name=badge_handler_name,
            app_card_badge_handler_name=app_card_badge_handler_name,
            action_handler_name=action_handler_name,
        )


class ModWebSettingControlKind(Enum):
    BOOLEAN_SWITCH = "boolean_switch"
    CHOICE_SELECT = "choice_select"
    TEXT_INPUT = "text_input"


class ModWebConfigEditorShape(Enum):
    SINGLE_FILE = "single_file"
    SINGLE_FOLDER_MULTI_FILE = "single_folder_multi_file"
    MULTI_FOLDER_SINGLE_FILE = "multi_folder_single_file"
    MULTI_FOLDER_MULTI_FILE = "multi_folder_multi_file"


@dataclass(frozen=True, slots=True)
class ModWebConfigEditorLayout:
    shape: ModWebConfigEditorShape
    selected_root_id: str
    selected_config_id: str

    @property
    def shows_root_selector(self) -> bool:
        return self.shape is ModWebConfigEditorShape.MULTI_FOLDER_MULTI_FILE

    @property
    def shows_file_selector(self) -> bool:
        return self.shape is not ModWebConfigEditorShape.SINGLE_FILE

    @property
    def primary_selector_label(self) -> str:
        if self.shape is ModWebConfigEditorShape.SINGLE_FOLDER_MULTI_FILE:
            return "File"
        if self.shape is ModWebConfigEditorShape.MULTI_FOLDER_SINGLE_FILE:
            return "Config Area"
        if self.shape is ModWebConfigEditorShape.MULTI_FOLDER_MULTI_FILE:
            return "File"
        raise ValueError("Single-file config layout does not use a selector.")


@dataclass(frozen=True, slots=True)
class ModWebHomeNodeSummary:
    node: ModWebNodeLink
    app_count: int
    system_summary: NodeSystemSummary | None


__all__: tuple[str, ...] = (
    "ChatMediaPreviewKind",
    "ModDownloadKind",
    "ModWebAppLink",
    "ModWebAppSectionKind",
    "ModWebAppTabContext",
    "ModWebAppTabDefinition",
    "ModWebAppTabSettingSnapshot",
    "ModWebAppTabVisibilityKind",
    "ModWebAppTabVisibilityRule",
    "ModWebBasePageModel",
    "ModWebConfigEditorLayout",
    "ModWebConfigEditorShape",
    "ModWebDirectUploadTarget",
    "ModWebHomeNodeSummary",
    "ModWebNotificationTrayItemKind",
    "ModWebNotificationTrayItemState",
    "ModWebNodeAppSection",
    "ModWebNodeLink",
    "ModWebNodeStatus",
    "ModWebOverviewPageModel",
    "ModWebPageModel",
    "ModWebSearchOption",
    "ModWebSevenDaysSandboxOptionEntry",
    "ModWebSevenDaysSandboxOptionsSummary",
    "ModWebSettingControlKind",
    "ModWebTitleStat",
    "ModWebTitleStatLine",
    "_ChatMediaPreview",
    "_ModWebAppCardBadgeSpec",
    "_ModWebAppHeroCornerBindings",
    "_ModWebAppHeroRuntimeDetails",
    "_ModWebAppRuntimeState",
    "_ModWebBadgeSpec",
    "_ModWebChatComposeRequest",
    "_ModWebChatEventGroup",
    "_ModWebChatPanelConfig",
    "_ModWebChatPanelSignal",
    "_ModWebChatSurfaceConfig",
    "_ModWebFakeChatMessageMode",
    "_ModWebFakeChatPreviewState",
    "_ModWebKillControlState",
    "_ModWebLinkSpec",
    "_ModWebLoginAdministrator",
    "_ModWebModToolbarBindings",
    "_ModWebNodePresenceBadgeSpec",
    "_ModWebNotificationPreviewSpec",
    "_ModWebNotificationTrayItem",
    "_ModWebRuntimeToolbarBindings",
    "_ModWebStartStopControlState",
    "_ModWebStatusPageConfig",
    "_ModWebTabActionSpec",
    "_SettingSecretConfig",
)
