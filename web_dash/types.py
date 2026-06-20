from __future__ import annotations

from mod_web_theme import BadgeTone

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
    badges: tuple[_ModWebBadgeSpec, ...] = ()
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

    def __post_init__(self) -> None:
        if not self.chat_changed and not self.runtime_changed:
            raise ValueError("Chat panel signal must refresh chat or runtime state.")
        if self.snapshot is not None and not self.chat_changed:
            raise ValueError("Chat panel signal snapshot requires chat refresh.")
        if self.app_stats is not None and not self.runtime_changed:
            raise ValueError("Chat panel signal runtime payload requires runtime refresh.")

    @classmethod
    def chat(cls, *, snapshot: NodeChatRoomSnapshot | None = None) -> "_ModWebChatPanelSignal":
        return cls(chat_changed=True, snapshot=snapshot)

    @classmethod
    def runtime(cls, *, app_stats: NodeAppRuntimeSummary | None = None) -> "_ModWebChatPanelSignal":
        return cls(runtime_changed=True, app_stats=app_stats)

    @classmethod
    def both(
        cls,
        *,
        snapshot: NodeChatRoomSnapshot | None = None,
        app_stats: NodeAppRuntimeSummary | None = None,
    ) -> "_ModWebChatPanelSignal":
        return cls(
            chat_changed=True,
            runtime_changed=True,
            snapshot=snapshot,
            app_stats=app_stats,
        )


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
class ModWebTitleStat:
    label: str
    value: str
    tone: BadgeTone = "grey"
    lines: tuple["ModWebTitleStatLine", ...] = ()


@dataclass(frozen=True, slots=True)
class ModWebTitleStatLine:
    label: str | None
    value: str


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
    mod_names: tuple[str, ...] = ()
    settings: tuple[ModWebAppTabSettingSnapshot, ...] = ()
    supports_map: bool = False
    supports_blueprints: bool = False

    def has_mod(self, mod_name: str) -> bool:
        target_name: str = mod_name.strip().casefold()
        return any(candidate.casefold() == target_name for candidate in self.mod_names)

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
    "ModWebHomeNodeSummary",
    "ModWebNotificationTrayItemKind",
    "ModWebNotificationTrayItemState",
    "ModWebNodeAppSection",
    "ModWebNodeLink",
    "ModWebNodeStatus",
    "ModWebOverviewPageModel",
    "ModWebPageModel",
    "ModWebSearchOption",
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
    "_ModWebModToolbarBindings",
    "_ModWebNodePresenceBadgeSpec",
    "_ModWebNotificationTrayItem",
    "_ModWebRuntimeToolbarBindings",
    "_ModWebStartStopControlState",
    "_ModWebStatusPageConfig",
    "_ModWebTabActionSpec",
    "_SettingSecretConfig",
)
