"""Shared dependencies, constants, and models for split status UI mixins."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from fastapi.exceptions import RequestValidationError
from hikari import Snowflake

from _async_utils import run_blocking
from _utils import Utilities
from currency_conversion import CurrencyConverter
from relay_notices import (
    AppLifecycleNotice,
    AppLifecycleState,
    BotLifecycleNotice,
    BotLifecycleStage,
    GameDeathKind,
    GameDeathNotice,
    GameEventNotice,
    GameProgressKind,
    GameProgressNotice,
    MaintenanceNotice,
    MaintenanceStage,
    PlayerSessionAction,
    PlayerSessionNotice,
    RelayNotice,
    RelayNoticeSource,
    render_notice_text,
)
from restart_targets import RestartTarget
from standard_drinks import (
    beverage_standard_drink_estimate,
    format_standard_drink_definition,
    format_standard_drink_number,
    format_standard_drink_range,
    parse_standard_drink_expression,
    standard_drink_conversion,
    standard_drink_definition,
    standard_drink_equivalents,
    standard_drink_units,
)
from unit_conversion import (
    UnitCategory,
    UnitConversion,
    UnitDefinition,
    convert_unit_category,
    display_units_for_category,
    format_unit_amount,
    parse_unit_amount,
    unit_categories,
    unit_definition,
    units_for_category,
)

from . import avatars as mod_web_avatars
from .constants import (
    _APP_ACTION_NOTIFICATION_TIMEOUT_MILLISECONDS,
    _MOD_WEB_REPOSITORY_URL,
    log,
)
from .nicegui_protocols import (
    ModWebUi,
    ModWebValueContainer,
    WebChatRelayPublisher,
    _value_as_object,
    _value_as_text,
)
from .runtime_imports import (
    MOD_WEB_ACTION_BASE_CLASSES,
    Awaitable,
    Button,
    Callable,
    ChatAttachment,
    ChatAuthor,
    ChatAuthorKind,
    ChatEmbed,
    ChatEndpointId,
    ChatEndpointKind,
    ChatEvent,
    ChatHub,
    ChatLink,
    ChatMediaProvider,
    ChatMessageReference,
    ChatReferenceKind,
    Iterable,
    Label,
    LiteralString,
    Mapping,
    ModWebSessionPersistence,
    ModWebUser,
    Path,
    Power_Level,
    Request,
    StarletteResponse,
    aiohttp,
    asyncio,
    cast,
    config,
    inspect,
    json,
    lru_cache,
    requests,
    urlencode,
)
from .service_base import ModWebServiceSupport
from .status_appearance import (
    _USER_APPEARANCE_COLOR_SPECS,
    ModWebUserAppearanceMixin,
    _UserAppearanceColorKey,
)
from .types import (
    ModWebNodeStatus,
    ModWebNotificationTrayItemKind,
    ModWebNotificationTrayItemState,
    RemoteNodeCircuitOpenError,
    _ModWebChatEventGroup,
    _ModWebFakeChatMessageMode,
    _ModWebFakeChatPreviewState,
    _ModWebLinkSpec,
    _ModWebLoginAdministrator,
    _ModWebNodePresenceBadgeSpec,
    _ModWebNotificationPreviewSpec,
    _ModWebNotificationTrayItem,
    _ModWebStatusPageConfig,
)
from .ui_helpers import ModWebUiHelpersMixin
from .user_settings import ModWebAppearanceSettings, ModWebChatSettings, ModWebTimestampSettings, ModWebUserSettings

if TYPE_CHECKING:
    from nicegui.elements.button import Button

_STATUS_SVG_DIRECTORY: Path = Path(__file__).resolve().parent.parent / "resources" / "svg" / "web_dash"
_TRANSFER_OVERLAY_TRACK_HEIGHT_REM = 0.38
_USER_HEADER_SURFACE_MIN_HEIGHT_REM = 4.9
_USER_HEADER_ICON_BUTTON_CLASSES = (
    "mod-list-button secondary mod-user-header-icon-button px-3 py-2"
)
_LOGIN_ADMINISTRATOR_LEVELS: tuple[Power_Level, ...] = (
    Power_Level.root,
    Power_Level.sudo,
    Power_Level.admin,
)
# Historical ACL members who cannot serve as active support contacts.
_LOGIN_CONTACT_EXCLUDED_USER_IDS: frozenset[int] = frozenset({792_857_784_508_219_404})
_DISCORD_SNOWFLAKE_MAX = int(Snowflake.max())


@dataclass(slots=True)
class _AliasDraft:
    display_name: str = ""
    add_alias: str = ""
    app_aliases: dict[str, str] = field(default_factory=dict)
    steam_id: str = ""
    minecraft_uuid: str = ""



@lru_cache(maxsize=None)
def _status_svg_markup(file_name: str, fallback_name: str | None = None) -> str:
    path: Path = _STATUS_SVG_DIRECTORY / file_name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        if fallback_name is None:
            raise
        fallback_path: Path = _STATUS_SVG_DIRECTORY / fallback_name
        return fallback_path.read_text(encoding="utf-8").strip()


class ModWebStatusFeatureSupport(ModWebServiceSupport):
    """Typed base for independent status features composed by the service."""


# Explicitly shared implementation contract for feature mixins.
__all__ = (
    "Any",
    "AppLifecycleNotice",
    "AppLifecycleState",
    "Awaitable",
    "BotLifecycleNotice",
    "BotLifecycleStage",
    "Button",
    "Callable",
    "ChatAttachment",
    "ChatAuthor",
    "ChatAuthorKind",
    "ChatEmbed",
    "ChatEndpointId",
    "ChatEndpointKind",
    "ChatEvent",
    "ChatHub",
    "ChatLink",
    "ChatMediaProvider",
    "ChatMessageReference",
    "ChatReferenceKind",
    "CurrencyConverter",
    "Decimal",
    "GameDeathKind",
    "GameDeathNotice",
    "GameEventNotice",
    "GameProgressKind",
    "GameProgressNotice",
    "Iterable",
    "Label",
    "Literal",
    "LiteralString",
    "MOD_WEB_ACTION_BASE_CLASSES",
    "MaintenanceNotice",
    "MaintenanceStage",
    "Mapping",
    "ModWebAppearanceSettings",
    "ModWebChatSettings",
    "ModWebNodeStatus",
    "ModWebNotificationTrayItemKind",
    "ModWebNotificationTrayItemState",
    "ModWebServiceSupport",
    "ModWebSessionPersistence",
    "ModWebStatusFeatureSupport",
    "ModWebTimestampSettings",
    "ModWebUi",
    "ModWebUiHelpersMixin",
    "ModWebUser",
    "ModWebUserAppearanceMixin",
    "ModWebUserSettings",
    "ModWebValueContainer",
    "Path",
    "PlayerSessionAction",
    "PlayerSessionNotice",
    "Power_Level",
    "RelayNotice",
    "RelayNoticeSource",
    "RemoteNodeCircuitOpenError",
    "Request",
    "RequestValidationError",
    "RestartTarget",
    "Snowflake",
    "StarletteResponse",
    "TYPE_CHECKING",
    "Utilities",
    "UnitCategory",
    "UnitConversion",
    "UnitDefinition",
    "WebChatRelayPublisher",
    "_APP_ACTION_NOTIFICATION_TIMEOUT_MILLISECONDS",
    "_AliasDraft",
    "_DISCORD_SNOWFLAKE_MAX",
    "_LOGIN_ADMINISTRATOR_LEVELS",
    "_LOGIN_CONTACT_EXCLUDED_USER_IDS",
    "_MOD_WEB_REPOSITORY_URL",
    "_ModWebChatEventGroup",
    "_ModWebFakeChatMessageMode",
    "_ModWebFakeChatPreviewState",
    "_ModWebLinkSpec",
    "_ModWebLoginAdministrator",
    "_ModWebNodePresenceBadgeSpec",
    "_ModWebNotificationPreviewSpec",
    "_ModWebNotificationTrayItem",
    "_ModWebStatusPageConfig",
    "_STATUS_SVG_DIRECTORY",
    "_TRANSFER_OVERLAY_TRACK_HEIGHT_REM",
    "_USER_APPEARANCE_COLOR_SPECS",
    "_USER_HEADER_ICON_BUTTON_CLASSES",
    "_USER_HEADER_SURFACE_MIN_HEIGHT_REM",
    "_UserAppearanceColorKey",
    "_status_svg_markup",
    "_value_as_object",
    "_value_as_text",
    "aiohttp",
    "asyncio",
    "beverage_standard_drink_estimate",
    "cast",
    "config",
    "convert_unit_category",
    "display_units_for_category",
    "dataclass",
    "date",
    "datetime",
    "field",
    "format_standard_drink_definition",
    "format_standard_drink_number",
    "format_standard_drink_range",
    "format_unit_amount",
    "inspect",
    "json",
    "log",
    "lru_cache",
    "mod_web_avatars",
    "parse_standard_drink_expression",
    "parse_unit_amount",
    "render_notice_text",
    "requests",
    "run_blocking",
    "standard_drink_conversion",
    "standard_drink_definition",
    "standard_drink_equivalents",
    "standard_drink_units",
    "timedelta",
    "timezone",
    "tzinfo",
    "unit_categories",
    "unit_definition",
    "units_for_category",
    "urlencode",
)
