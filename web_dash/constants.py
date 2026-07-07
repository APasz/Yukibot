from __future__ import annotations

from .runtime_imports import TYPE_CHECKING, Logger, Pattern, config, logging, re

if TYPE_CHECKING:
    from nicegui.elements.codemirror.codemirror import SUPPORTED_LANGUAGES, SUPPORTED_THEMES


log: Logger = logging.getLogger(__name__)
traffic_log: Logger = logging.getLogger(config.LOGGER_TRAFFIC)

_MOD_WEB_PAGE_PATH = "/mod-web/mods/{app_name}"
_MOD_WEB_REPOSITORY_URL = "https://github.com/APasz/Yukibot"
_MOD_WEB_STARTUP_TIMEOUT_SECONDS: float = 10.0
_REMOTE_NODE_TOKEN_TTL_SECONDS = 5 * 60
_DIRECT_UPLOAD_TOKEN_REFRESH_SECONDS: float = _REMOTE_NODE_TOKEN_TTL_SECONDS / 2
_REMOTE_NODE_REQUEST_TIMEOUT_SECONDS: float = 15.0
_REMOTE_NODE_LONG_MUTATION_TIMEOUT_SECONDS: float = 10 * 60
_BULK_METADATA_REQUEST_TIMEOUT_SECONDS: float = 10 * 60
_REMOTE_NODE_GET_MAX_ATTEMPTS: int = 2
_REMOTE_NODE_GET_RETRY_DELAY_SECONDS: float = 0.2
_REMOTE_NODE_PRESENCE_CONNECT_TIMEOUT_SECONDS: float = 2.0
_REMOTE_NODE_PRESENCE_READ_TIMEOUT_SECONDS: float = 4.0
_REMOTE_NODE_PRESENCE_REQUEST_TIMEOUT: tuple[float, float] = (
    _REMOTE_NODE_PRESENCE_CONNECT_TIMEOUT_SECONDS,
    _REMOTE_NODE_PRESENCE_READ_TIMEOUT_SECONDS,
)
_HOME_NODE_LATENCY_REFRESH_INTERVAL_SECONDS: float = 15.0
_HOME_NODE_LATENCY_TIMEOUT_SECONDS: float = 4.0
_NODE_PRESENCE_RECONNECT_DELAY_SECONDS: float = 2.0
_REMOTE_CHAT_STREAM_RECONNECT_DELAY_SECONDS: float = 2.0
_REMOTE_CHAT_STREAM_HEARTBEAT_SECONDS: float = 30.0
_DOWNLOAD_FEEDBACK_DELAY_SECONDS: float = 0.15
_APP_ACTION_NOTIFICATION_TIMEOUT_MILLISECONDS: int = 10_000
_TITLE_STATS_REFRESH_INTERVAL_SECONDS: float = 5.0
_APP_RUNTIME_REFRESH_INTERVAL_SECONDS: float = 2.0
_CHAT_HISTORY_REFRESH_INTERVAL_SECONDS: float = 2.0
_CHAT_HISTORY_LIMIT = 100
_CHAT_GROUP_WINDOW_SECONDS: float = 120.0
_CHAT_TIMELINE_BOTTOM_THRESHOLD_PX = 24
_SEARCH_INPUT_DEBOUNCE_MILLISECONDS = 200
_WEB_CHAT_MESSAGE_MAX_LENGTH = 1000
_SAME_ORIGIN_NODE_API_BASE = "/api/node"
_SAME_ORIGIN_NODE_PROXY_BASE = "/api/node-proxy"
_APP_LIST_API_QUERY_PARAM = "dev_api"
_APP_SECTION_QUERY_PARAM = "tab"
_APP_SEARCH_QUERY_PARAM = "search"
_APP_MOD_SORT_QUERY_PARAM = "mod_sort"
_DEV_SIMULATED_DOWN_NODE_QUERY_PARAM = "dev_node_down"
_HIDDEN_SETTING_GLYPHS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789#$%&*!?"
_HIDDEN_SETTING_CYCLE_VARIANT_COUNT = 4
_CONFIG_EDITOR_THEME: "SUPPORTED_THEMES" = "vscodeDark"
_CONFIG_EDITOR_DOCKERFILE_LANGUAGE: "SUPPORTED_LANGUAGES" = "Dockerfile"
_CONFIG_EDITOR_LANGUAGE_BY_SUFFIX: dict[str, "SUPPORTED_LANGUAGES"] = {
    ".cfg": "Properties files",
    ".conf": "Properties files",
    ".ini": "Properties files",
    ".json": "JSON",
    ".md": "Markdown",
    ".properties": "Properties files",
    ".sh": "Shell",
    ".toml": "TOML",
    ".xml": "XML",
    ".yaml": "YAML",
    ".yml": "YAML",
}
_CHAT_MEDIA_IMAGE_EXTENSIONS: set[str] = {".apng", ".avif", ".gif", ".jpg", ".jpeg", ".png", ".svg", ".webp"}
_CHAT_MEDIA_VIDEO_EXTENSIONS: set[str] = {".m4v", ".mov", ".mp4", ".ogg", ".ogv", ".webm"}
_CHAT_MEDIA_AUDIO_EXTENSIONS: set[str] = {".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav", ".weba"}
_CHAT_MARKUP_CODE_BLOCK_RE: Pattern[str] = re.compile(r"```(?:[^\n`]*)\n?(.*?)```", re.DOTALL)
_CHAT_MARKUP_INLINE_CODE_RE: Pattern[str] = re.compile(r"`([^`\n]+)`")
_CHAT_MARKUP_ESCAPE_RE: Pattern[str] = re.compile(r"\\(?P<escaped>[\\`*_{}\[\]()#+\-.!>|~])")
_CHAT_MARKUP_LINK_RE: Pattern[str] = re.compile(r"(?<!!)\[(?P<label>[^\]\n]+)\]\((?P<url>[^)\s]+)\)")
_CHAT_MARKUP_RAW_URL_RE: Pattern[str] = re.compile(r"(?P<url>https?://[^\s<]+)")
_CHAT_MARKUP_BOLD_RE: Pattern[str] = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_CHAT_MARKUP_UNDERLINE_RE: Pattern[str] = re.compile(r"__(?=\S)(.+?)(?<=\S)__")
_CHAT_MARKUP_STRIKETHROUGH_RE: Pattern[str] = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_CHAT_MARKUP_ITALIC_STAR_RE: Pattern[str] = re.compile(r"(?<!\*)\*(?=\S)(.+?)(?<=\S)\*(?!\*)")
_CHAT_MARKUP_ITALIC_UNDERSCORE_RE: Pattern[str] = re.compile(r"(?<!_)_(?=\S)(.+?)(?<=\S)_(?!_)")
_CHAT_MARKUP_SPOILER_RE: Pattern[str] = re.compile(r"\|\|(.+?)\|\|")
_CHAT_MARKUP_DISCORD_MENTION_RE: Pattern[str] = re.compile(
    r"<@!?(?P<discord_user_id>\d+)>|(?<![\w</])@(?P<raw_discord_user_id>\d+)\b"
)
_CHAT_MARKUP_DISCORD_CHANNEL_RE: Pattern[str] = re.compile(r"<#(?P<channel_id>\d+)>")
_CHAT_MARKUP_DISCORD_ROLE_RE: Pattern[str] = re.compile(r"<@&(?P<role_id>\d+)>")
_CHAT_MARKUP_DISCORD_TIMESTAMP_RE: Pattern[str] = re.compile(r"<t:(?P<unix>\d+)(?::(?P<style>[tTdDfFR]))?>")
_CHAT_MARKUP_HEADER_RE: Pattern[str] = re.compile(r"^(?P<level>#{1,3}) (?P<content>.+)$")
_CHAT_MARKUP_ORDERED_LIST_RE: Pattern[str] = re.compile(r"^(?P<indent> *)(?P<number>\d+)\. (?P<content>.+)$")
_CHAT_MARKUP_SUBTEXT_RE: Pattern[str] = re.compile(r"^-# (?P<content>.+)$")
_CHAT_MARKUP_UNORDERED_LIST_RE: Pattern[str] = re.compile(r"^(?P<indent> *)(?P<marker>[-*]) (?P<content>.+)$")


__all__: tuple[str, ...] = (
    "log",
    "traffic_log",
    "_APP_LIST_API_QUERY_PARAM",
    "_APP_ACTION_NOTIFICATION_TIMEOUT_MILLISECONDS",
    "_APP_RUNTIME_REFRESH_INTERVAL_SECONDS",
    "_APP_MOD_SORT_QUERY_PARAM",
    "_APP_SECTION_QUERY_PARAM",
    "_APP_SEARCH_QUERY_PARAM",
    "_CHAT_GROUP_WINDOW_SECONDS",
    "_BULK_METADATA_REQUEST_TIMEOUT_SECONDS",
    "_CHAT_HISTORY_LIMIT",
    "_CHAT_HISTORY_REFRESH_INTERVAL_SECONDS",
    "_CHAT_MARKUP_BOLD_RE",
    "_CHAT_MARKUP_CODE_BLOCK_RE",
    "_CHAT_MARKUP_DISCORD_MENTION_RE",
    "_CHAT_MARKUP_DISCORD_CHANNEL_RE",
    "_CHAT_MARKUP_DISCORD_ROLE_RE",
    "_CHAT_MARKUP_DISCORD_TIMESTAMP_RE",
    "_CHAT_MARKUP_ESCAPE_RE",
    "_CHAT_MARKUP_HEADER_RE",
    "_CHAT_MARKUP_INLINE_CODE_RE",
    "_CHAT_MARKUP_LINK_RE",
    "_CHAT_MARKUP_ORDERED_LIST_RE",
    "_CHAT_MARKUP_RAW_URL_RE",
    "_CHAT_MARKUP_SUBTEXT_RE",
    "_CHAT_MARKUP_UNORDERED_LIST_RE",
    "_CHAT_MARKUP_ITALIC_STAR_RE",
    "_CHAT_MARKUP_ITALIC_UNDERSCORE_RE",
    "_CHAT_MARKUP_SPOILER_RE",
    "_CHAT_MARKUP_STRIKETHROUGH_RE",
    "_CHAT_MARKUP_UNDERLINE_RE",
    "_CHAT_MEDIA_AUDIO_EXTENSIONS",
    "_CHAT_MEDIA_IMAGE_EXTENSIONS",
    "_CHAT_MEDIA_VIDEO_EXTENSIONS",
    "_CHAT_TIMELINE_BOTTOM_THRESHOLD_PX",
    "_CONFIG_EDITOR_DOCKERFILE_LANGUAGE",
    "_CONFIG_EDITOR_LANGUAGE_BY_SUFFIX",
    "_CONFIG_EDITOR_THEME",
    "_DEV_SIMULATED_DOWN_NODE_QUERY_PARAM",
    "_DIRECT_UPLOAD_TOKEN_REFRESH_SECONDS",
    "_DOWNLOAD_FEEDBACK_DELAY_SECONDS",
    "_HOME_NODE_LATENCY_REFRESH_INTERVAL_SECONDS",
    "_HOME_NODE_LATENCY_TIMEOUT_SECONDS",
    "_NODE_PRESENCE_RECONNECT_DELAY_SECONDS",
    "_HIDDEN_SETTING_CYCLE_VARIANT_COUNT",
    "_HIDDEN_SETTING_GLYPHS",
    "_MOD_WEB_PAGE_PATH",
    "_MOD_WEB_REPOSITORY_URL",
    "_MOD_WEB_STARTUP_TIMEOUT_SECONDS",
    "_REMOTE_CHAT_STREAM_HEARTBEAT_SECONDS",
    "_REMOTE_CHAT_STREAM_RECONNECT_DELAY_SECONDS",
    "_REMOTE_NODE_PRESENCE_CONNECT_TIMEOUT_SECONDS",
    "_REMOTE_NODE_PRESENCE_READ_TIMEOUT_SECONDS",
    "_REMOTE_NODE_PRESENCE_REQUEST_TIMEOUT",
    "_REMOTE_NODE_GET_MAX_ATTEMPTS",
    "_REMOTE_NODE_GET_RETRY_DELAY_SECONDS",
    "_REMOTE_NODE_LONG_MUTATION_TIMEOUT_SECONDS",
    "_REMOTE_NODE_REQUEST_TIMEOUT_SECONDS",
    "_REMOTE_NODE_TOKEN_TTL_SECONDS",
    "_SAME_ORIGIN_NODE_API_BASE",
    "_SAME_ORIGIN_NODE_PROXY_BASE",
    "_SEARCH_INPUT_DEBOUNCE_MILLISECONDS",
    "_TITLE_STATS_REFRESH_INTERVAL_SECONDS",
    "_WEB_CHAT_MESSAGE_MAX_LENGTH",
)
