from __future__ import annotations

from .runtime_imports import (
    Awaitable,
    Button,
    Callable,
    ChatAuthorKind,
    ChatEndpointKind,
    ChatEvent,
    Enum,
    Literal,
    NodeAppMutationAction,
    NodeAppRuntimeSummary,
    NodeAppTransitionState,
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
from mod_web_theme import BadgeTone

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


@dataclass(frozen=True, slots=True)
class _ModWebLinkSpec:
    label: str
    url: str


@dataclass(frozen=True, slots=True)
class _ModWebAppRuntimeState:
    running: bool
    enabled: bool
    transition_state: NodeAppTransitionState
    player_count: int | None
    player_capacity: int | None


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
class _ModWebModToolbarBindings:
    selection_button: Button | None
    download_button: Button | None


@dataclass(frozen=True, slots=True)
class _ModWebStatusPageConfig:
    title: str
    support_text: str
    badge_text: str
    badge_tone: BadgeTone
    accent_color_hex: str
    icon_markup: str | None = None
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
    EMBED = "embed"


@dataclass(slots=True)
class _ModWebFakeChatPreviewState:
    app_name: str | None = None
    source_kind: ChatEndpointKind = ChatEndpointKind.APP
    author_kind: ChatAuthorKind = ChatAuthorKind.GAME_PLAYER
    author_name: str = "Alex"
    message_mode: _ModWebFakeChatMessageMode = _ModWebFakeChatMessageMode.TEXT
    content_text: str = "hello from preview"
    detail_text: str = "Skeleton"
    embed_title: str = "Advancement"
    embed_description: str = "Stone Age"
    source_label: str = ""


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
    saves_api_url: str | None = None
    settings_api_url: str | None = None
    supports_console_actions: bool = field(default=False, kw_only=True)
    supports_chat: bool = field(default=False, kw_only=True)
    chat_url: str | None = field(default=None, kw_only=True)
    runtime_changed: bool = field(default=False, kw_only=True)


@dataclass(frozen=True, slots=True)
class ModWebNodeLink:
    node_name: str
    label: str
    url: str
    api_base_url: str
    api_url: str
    is_current: bool


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
    console_actions: NodeConsoleActionList | None = field(default=None, kw_only=True)
    supports_chat: bool = field(default=False, kw_only=True)
    chat_url: str | None = field(default=None, kw_only=True)


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
    MODS = "mods"
    CONFIGS = "configs"
    SETTINGS = "settings"
    SAVES = "saves"
    CONSOLE = "console"

    @property
    def label(self) -> str:
        if self is ModWebAppSectionKind.MODS:
            return "Mods"
        if self is ModWebAppSectionKind.CONFIGS:
            return "Configs"
        if self is ModWebAppSectionKind.SETTINGS:
            return "Settings"
        if self is ModWebAppSectionKind.SAVES:
            return "Saves"
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
    "ModWebBasePageModel",
    "ModWebConfigEditorLayout",
    "ModWebConfigEditorShape",
    "ModWebHomeNodeSummary",
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
    "_ModWebAppRuntimeState",
    "_ModWebBadgeSpec",
    "_ModWebChatComposeRequest",
    "_ModWebChatEventGroup",
    "_ModWebChatPanelConfig",
    "_ModWebChatPanelSignal",
    "_ModWebFakeChatMessageMode",
    "_ModWebFakeChatPreviewState",
    "_ModWebKillControlState",
    "_ModWebLinkSpec",
    "_ModWebModToolbarBindings",
    "_ModWebRuntimeToolbarBindings",
    "_ModWebStartStopControlState",
    "_ModWebStatusPageConfig",
    "_SettingSecretConfig",
)
