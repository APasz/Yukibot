from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi.exceptions import RequestValidationError

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
    lru_cache,
    requests,
    urlencode,
)
from .service_base import ModWebServiceSupport
from .types import (
    ModWebNodeStatus,
    ModWebNotificationTrayItemKind,
    ModWebNotificationTrayItemState,
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

if TYPE_CHECKING:
    from nicegui.element import Element

_STATUS_SVG_DIRECTORY: Path = Path(__file__).resolve().parent.parent / "resources" / "svg" / "web_dash"
_USER_HEADER_SURFACE_MIN_HEIGHT_REM = 4.9
_USER_HEADER_TRAY_CARD_HEIGHT_REM = 4.35
_USER_HEADER_SURFACE_ASPECT_RATIO = "4 / 1"
_USER_HEADER_STACK_WIDTH_REM = 10.5
_USER_HEADER_ICON_BUTTON_CLASSES = "mod-list-button secondary grow basis-0 min-h-[2.2rem] min-w-0 px-3 py-2"
_LOGIN_ADMINISTRATOR_LEVELS: tuple[Power_Level, ...] = (
    Power_Level.root,
    Power_Level.sudo,
    Power_Level.admin,
)
# Historical ACL members who cannot serve as active support contacts.
_LOGIN_CONTACT_EXCLUDED_USER_IDS: frozenset[int] = frozenset({792_857_784_508_219_404})


@dataclass(slots=True)
class _AliasDialogDraft:
    display_name: str = ""
    add_alias: str = ""
    app_aliases: dict[str, str] = field(default_factory=dict)
    steam_id: str = ""
    minecraft_uuid: str = ""


class ModWebStatusMixin(ModWebServiceSupport):
    @staticmethod
    def _fake_chat_preview_notice_source(source_kind: ChatEndpointKind) -> RelayNoticeSource:
        if source_kind is ChatEndpointKind.APP:
            return RelayNoticeSource.APP_LOG
        if source_kind is ChatEndpointKind.DISCORD_CHANNEL:
            return RelayNoticeSource.DISCORD
        if source_kind is ChatEndpointKind.WEB_SESSION:
            return RelayNoticeSource.WEB
        return RelayNoticeSource.BOT

    def _render_error_page(self, *, ui: ModWebUi, title: str, detail: str, app_name: str | None = None) -> None:
        icon_markup: str = self._error_page_icon_markup(title)
        self._apply_theme(ui=ui)
        with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
            self._render_status_page_panel(
                ui=ui,
                config=_ModWebStatusPageConfig(
                    title=title,
                    support_text="The requested mod web view could not be rendered.",
                    badge_text="Unavailable",
                    badge_tone="red",
                    accent_color_hex="#dc2626",
                    icon_markup=icon_markup,
                    detail_text=detail,
                    detail_label="Details",
                    context_label=app_name,
                    actions=(_ModWebLinkSpec(label="Home", url=self.index_path()),),
                ),
            )

    def _render_remote_node_unavailable_page(
        self,
        *,
        ui: ModWebUi,
        node_name: str,
        exception: Exception,
        retry_url: str | None = None,
    ) -> None:
        self._apply_theme(ui=ui)
        with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
            self._render_status_page_panel(
                ui=ui,
                config=_ModWebStatusPageConfig(
                    title="Node reconnecting" if retry_url is not None else "Node unavailable",
                    support_text=(
                        "The node is restarting or temporarily offline. This page will reconnect automatically."
                        if retry_url is not None
                        else self._friendly_remote_node_error_text(exception)
                    ),
                    badge_text="Reconnecting" if retry_url is not None else "Unavailable",
                    badge_tone="warn" if retry_url is not None else "red",
                    accent_color_hex="#f59e0b" if retry_url is not None else "#dc2626",
                    icon_markup=self._remote_node_unavailable_icon_markup(),
                    context_label=node_name,
                    actions=(
                        *((_ModWebLinkSpec(label="Retry", url=retry_url),) if retry_url is not None else ()),
                        _ModWebLinkSpec(label="Home", url=self.index_path()),
                    ),
                ),
            )

    def _render_node_unavailable_card(self, *, ui: ModWebUi, message: str) -> None:
        with ui.card().classes("mod-card mod-card-empty w-full"):
            with ui.row().classes("w-full items-start gap-4 p-5 flex-wrap"):
                ui.html(self._remote_node_unavailable_icon_markup()).classes("shrink-0")
                with ui.column().classes("min-w-0 gap-1"):
                    ui.label("Node unavailable").classes("text-base font-bold mod-title-small")
                    ui.label(message).classes("text-sm mod-subtitle break-words")
                    ui.label("Yuki will keep trying in the background.").classes(
                        "text-xs uppercase tracking-[0.18em] mod-subtitle"
                    )

    def _render_status_page_panel(self, *, ui: ModWebUi, config: _ModWebStatusPageConfig) -> None:
        with (
            ui.card()
            .classes(f"{self._hero_card_classes()} mod-status-card")
            .style(self._hero_card_style(config.accent_color_hex))
        ):
            with ui.column().classes(f"{self._hero_shell_classes()} mod-status-shell"):
                with ui.row().classes("mod-status-top w-full items-start justify-between gap-4 flex-wrap"):
                    with ui.column().classes("mod-status-content min-w-0 gap-4"):
                        with ui.column().classes(f"{self._hero_header_main_classes()} mod-status-header-main"):
                            with ui.row().classes("mod-status-kicker w-full items-center gap-2 flex-wrap"):
                                self._badge(ui=ui, text=config.badge_text, tone=config.badge_tone)
                                if config.context_label is not None:
                                    ui.label(config.context_label).classes("mod-status-context break-all")
                            ui.label(config.title).classes(self._hero_title_classes())
                            ui.label(config.support_text).classes(self._hero_support_classes())
                        if config.detail_text is not None:
                            with ui.column().classes("mod-status-detail w-full"):
                                if config.detail_label is not None:
                                    ui.label(config.detail_label).classes("mod-status-detail-label")
                                ui.label(config.detail_text).classes("mod-status-detail-text break-all")
                        if config.actions:
                            with ui.row().classes("mod-status-actions w-full"):
                                for action in config.actions:
                                    self._action_link(
                                        ui=ui,
                                        label=action.label,
                                        url=action.url,
                                        new_tab=action.new_tab,
                                    )
                    ui.html(self._resolved_status_icon_markup(config)).classes("mod-status-figure mod-status-figure-inline")

    def _render_framework_page_exception(self, *, ui: ModWebUi, exception: Exception) -> None:
        self._apply_theme(ui=ui)
        with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
            self._render_status_page_panel(
                ui=ui,
                config=self._framework_http_error_config(status_code=500, exception=exception),
            )

    async def _build_framework_error_response(
        self,
        *,
        ui: ModWebUi,
        request: Request,
        status_code: int,
        exception: Exception,
    ) -> StarletteResponse:
        from nicegui.client import Client
        from nicegui.page import page as nicegui_page

        with Client(nicegui_page(""), request=request) as client:
            self._apply_theme(ui=ui)
            with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
                self._render_status_page_panel(
                    ui=ui,
                    config=self._framework_http_error_config(status_code=status_code, exception=exception),
                )
        return client.build_response(request, status_code)

    def _framework_http_error_config(self, *, status_code: int, exception: Exception) -> _ModWebStatusPageConfig:
        if status_code == 310:
            return _ModWebStatusPageConfig(
                title="Too many redirects",
                support_text="The page resolved back to itself instead of rendering. Check the requested route and node routing configuration.",
                badge_text="310",
                badge_tone="warn",
                accent_color_hex="#f59e0b",
                icon_markup=self._framework_error_icon_markup(),
                detail_text=self._framework_http_error_detail_text(exception),
                detail_label="Details",
                actions=(_ModWebLinkSpec(label="Home", url=self.index_path()),),
            )
        if status_code == 404:
            return _ModWebStatusPageConfig(
                title="Page not found",
                support_text="The address does not match a mod web page.",
                badge_text="404",
                badge_tone="grey",
                accent_color_hex="#71717a",
                icon_markup=self._framework_error_icon_markup(),
                detail_text=self._framework_http_error_detail_text(exception),
                detail_label="Details",
                actions=(_ModWebLinkSpec(label="Home", url=self.index_path()),),
            )
        if 400 <= status_code < 500:
            title = "Request could not be completed"
            support_text = (
                "The server rejected this request. Review the details below, then return to the app and try again."
            )
            if status_code == 400:
                title = "Invalid request"
            elif status_code in {401, 403}:
                title = "Access denied"
            elif status_code == 429:
                title = "Too many requests"
                support_text = "The server received too many requests. Wait briefly, then try again."
            return _ModWebStatusPageConfig(
                title=title,
                support_text=support_text,
                badge_text=str(status_code),
                badge_tone="warn",
                accent_color_hex="#f59e0b",
                icon_markup=self._framework_error_icon_markup(),
                detail_text=self._framework_http_error_detail_text(exception),
                detail_label="Details",
                actions=(_ModWebLinkSpec(label="Home", url=self.index_path()),),
            )
        http_detail: object | None = getattr(exception, "detail", None)
        if isinstance(http_detail, str) and http_detail.strip():
            return _ModWebStatusPageConfig(
                title="Service unavailable" if status_code == 503 else "Server error",
                support_text="The server could not complete this request. Wait briefly, then try again.",
                badge_text=str(status_code),
                badge_tone="red",
                accent_color_hex="#dc2626",
                icon_markup=self._framework_error_icon_markup(),
                detail_text=http_detail.strip(),
                detail_label="Details",
                actions=(_ModWebLinkSpec(label="Home", url=self.index_path()),),
            )
        return _ModWebStatusPageConfig(
            title="Server error",
            support_text="The page failed while rendering. Refresh to retry or return home.",
            badge_text="500",
            badge_tone="red",
            accent_color_hex="#dc2626",
            icon_markup=self._framework_error_icon_markup(),
            detail_text=self._exception_detail_text(exception),
            detail_label="Exception",
            actions=(_ModWebLinkSpec(label="Home", url=self.index_path()),),
        )

    @staticmethod
    def _framework_http_error_detail_text(exception: Exception) -> str:
        detail: object | None = getattr(exception, "detail", None)
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if isinstance(exception, RequestValidationError):
            validation_details: list[str] = []
            for error in exception.errors():
                location = ".".join(str(part) for part in error["loc"])
                message = error["msg"].strip()
                validation_details.append(f"{location}: {message}" if location else message)
            if validation_details:
                return "\n".join(validation_details)
        return ModWebStatusMixin._exception_detail_text(exception)

    @staticmethod
    def _should_render_framework_error_page(*, method: str, path: str, accept_header: str | None) -> bool:
        if method != "GET":
            return False
        if path.startswith(("/_nicegui", "/static")):
            return False
        if accept_header is None:
            return False
        accept: str = accept_header.casefold()
        return "text/html" in accept

    @staticmethod
    async def _resolve_exception_handler_result(result: object) -> object:
        if inspect.isawaitable(result):
            return await cast(Awaitable[object], result)
        return result

    @staticmethod
    def _exception_detail_text(exception: Exception) -> str:
        detail: str = exception.__class__.__name__
        message: str = str(exception).strip()
        if message:
            return f"{detail}: {message}"
        return detail

    @staticmethod
    def _exception_chain(exception: BaseException) -> tuple[BaseException, ...]:
        chain: list[BaseException] = []
        current = exception
        seen_ids: set[int] = set()
        while current is not None and id(current) not in seen_ids:
            chain.append(current)
            seen_ids.add(id(current))
            current: BaseException | None = current.__cause__
        return tuple[BaseException, ...](chain)

    @classmethod
    def _friendly_remote_node_error_text(cls, exception: BaseException) -> str:
        for cause in cls._exception_chain(exception):
            if isinstance(cause, (requests.Timeout, asyncio.TimeoutError)):
                return "This node is taking too long to respond. It may be offline or still waking up."
            if isinstance(cause, (requests.ConnectionError, aiohttp.ClientConnectionError, ConnectionError)):
                return "This node is unreachable right now. It may be offline or still waking up."

        detail: str = str(exception).strip()
        if detail.startswith("Remote node rejected the request:"):
            return "This node answered, but it rejected Yuki's request."
        if detail == "Remote node returned invalid JSON." or detail.startswith("Remote node response must be"):
            return "This node answered, but it sent back something Yuki could not understand."
        return "Yuki could not talk to this node right now. Refresh to try again in a moment."

    @classmethod
    def _remote_node_error_is_transient(cls, exception: BaseException) -> bool:
        return any(
            isinstance(
                cause,
                (
                    requests.Timeout,
                    requests.ConnectionError,
                    aiohttp.ClientConnectionError,
                    asyncio.TimeoutError,
                    ConnectionError,
                ),
            )
            for cause in cls._exception_chain(exception)
        )

    @staticmethod
    def _remote_node_unavailable_icon_markup() -> str:
        return _status_svg_markup("remote_node_unavailable.svg", fallback_name="generic_error.svg")

    @staticmethod
    def _framework_error_icon_markup() -> str:
        return _status_svg_markup("framework_error.svg", fallback_name="generic_error.svg")

    @staticmethod
    def _generic_error_icon_markup() -> str:
        return _status_svg_markup("generic_error.svg")

    @staticmethod
    def _about_icon_markup() -> str:
        return _status_svg_markup("about.svg", fallback_name="generic_error.svg")

    @staticmethod
    def _chat_unavailable_icon_markup() -> str:
        return _status_svg_markup("chat_unavailable.svg", fallback_name="generic_error.svg")

    @staticmethod
    def _access_denied_icon_markup() -> str:
        return _status_svg_markup("access_denied.svg", fallback_name="generic_error.svg")

    @staticmethod
    def _page_unavailable_icon_markup() -> str:
        return _status_svg_markup("page_unavailable.svg", fallback_name="generic_error.svg")

    def _error_page_icon_markup(self, title: str) -> str:
        if title == "Chat unavailable":
            return self._chat_unavailable_icon_markup()
        if title == "Page unavailable":
            return self._page_unavailable_icon_markup()
        return self._generic_error_icon_markup()

    def _resolved_status_icon_markup(self, config: _ModWebStatusPageConfig) -> str:
        if config.icon_markup is not None:
            return config.icon_markup
        return self._generic_error_icon_markup()

    async def _authorised_page_user(
        self, *, ui: ModWebUi, request: Request, required_level: Power_Level
    ) -> ModWebUser | None:
        if not self._auth.enabled:
            self._render_auth_setup_page(ui=ui)
            return None
        user: ModWebUser | None = self._auth.current_user(request)
        if user is None:
            await self._render_login_page(
                ui=ui,
                next_path=self._request_path(request),
                request=request,
                show_api_actions=self._app_list_api_actions_enabled(request),
            )
            return None
        if self._acl is None:
            self._render_error_page(
                ui=ui,
                title="Permissions unavailable",
                detail="The mod web UI started without Access_Control attached.",
            )
            return None
        if not self._acl.can(user.discord_id, required_level):
            self._render_forbidden_page(ui=ui, user=user, required_level=required_level)
            return None
        return user

    def _render_auth_setup_page(self, *, ui: ModWebUi) -> None:
        self._apply_theme(ui=ui)
        with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
            self._render_status_page_panel(
                ui=ui,
                config=_ModWebStatusPageConfig(
                    title="Sign-in unavailable",
                    support_text="Configure Discord OAuth for the mod web UI before using this page.",
                    badge_text="Setup Required",
                    badge_tone="warn",
                    accent_color_hex="#f59e0b",
                    icon_markup=self._generic_error_icon_markup(),
                    detail_text=self._auth.redirect_url,
                    detail_label="Callback URL",
                    actions=(_ModWebLinkSpec(label="Home", url=self.index_path()),),
                ),
            )

    def _render_oauth_failure_page(self, *, ui: ModWebUi, detail: str) -> None:
        self._apply_theme(ui=ui)
        with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
            self._render_status_page_panel(
                ui=ui,
                config=self._oauth_failure_page_config(detail),
            )

    def _oauth_failure_page_config(self, detail: str) -> _ModWebStatusPageConfig:
        return _ModWebStatusPageConfig(
            title="Discord sign-in failed",
            support_text="Yukibot could not complete the Discord sign-in flow.",
            badge_text="Sign-in Failed",
            badge_tone="red",
            accent_color_hex="#dc2626",
            icon_markup=self._generic_error_icon_markup(),
            detail_text=detail,
            detail_label="Details",
            actions=(
                _ModWebLinkSpec(
                    label="Try Again",
                    url=f"/auth/login?{urlencode({'next_path': self.index_path()})}",
                ),
                _ModWebLinkSpec(label="Home", url=self.index_path()),
            ),
        )

    async def _render_login_page(
        self,
        *,
        ui: ModWebUi,
        next_path: str,
        request: Request,
        show_api_actions: bool,
    ) -> None:
        self._apply_theme(ui=ui)
        simulated_down_node_names: tuple[str, ...] = self._simulated_down_node_names(request)
        node_statuses: tuple[ModWebNodeStatus, ...] = await self._login_node_statuses_async(
            simulated_down_node_names=simulated_down_node_names
        )
        login_presence_badge_specs: list[_ModWebNodePresenceBadgeSpec] = []
        with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
            with ui.card().classes(self._hero_card_classes()):
                with ui.column().classes(self._hero_shell_classes()):
                    with ui.row().classes(self._hero_header_classes()):
                        with ui.column().classes(self._hero_header_main_classes()):
                            ui.label("Yukibot Web").classes(self._hero_title_classes())
                            ui.label(self._login_page_subtitle()).classes(self._hero_support_classes())
                        with ui.column().classes(self._hero_badges_classes()):
                            with ui.row().classes(self._hero_badge_row_classes()):
                                for status in node_statuses:
                                    badge = self._interactive_badge(
                                        ui=ui,
                                        text=self._login_node_status_badge_text(status),
                                        tone=self._login_node_status_badge_tone(status),
                                        extra_classes="mod-node-status-badge",
                                    )
                                    badge_element_id = getattr(badge, "id", None)
                                    if not isinstance(badge_element_id, int):
                                        continue
                                    login_presence_badge_specs.append(
                                        _ModWebNodePresenceBadgeSpec(
                                            node_name=status.node.node_name,
                                            badge_element_id=badge_element_id,
                                            text_element_id=None,
                                            node_label=status.node.label,
                                            pending_text=self._login_node_status_badge_text(status),
                                            alive_text=f"{status.node.label}: Alive",
                                            down_text=f"{status.node.label}: Down",
                                            presence_stream_url=(
                                                None if status.is_simulated_down else status.node.presence_stream_url
                                            ),
                                            pending_class_name=self._badge_class_name(
                                                tone=self._login_node_status_badge_tone(status),
                                                extra_classes="mod-node-status-badge",
                                            ),
                                            healthy_class_name=self._badge_class_name(
                                                tone="black",
                                                extra_classes="mod-node-status-badge",
                                            ),
                                            unhealthy_class_name=self._badge_class_name(
                                                tone="red",
                                                extra_classes="mod-node-status-badge",
                                            ),
                                        )
                                    )
                            self._run_node_presence_badges_javascript(
                                ui=ui,
                                badge_specs=tuple(login_presence_badge_specs),
                                controller_key="modWebLoginNodePresence",
                            )
                    login_show_api_actions: bool = show_api_actions
                    login_persistence = ModWebSessionPersistence.BROWSER_SESSION

                    def _set_login_show_api_actions(enabled: bool) -> None:
                        nonlocal login_show_api_actions
                        login_show_api_actions = enabled
                        _render_login_actions.refresh()

                    def _handle_login_show_api_actions_change(event: ModWebValueContainer) -> None:
                        _set_login_show_api_actions(bool(_value_as_object(event)))

                    def _handle_login_persistence_change(event: ModWebValueContainer) -> None:
                        nonlocal login_persistence
                        login_persistence = ModWebSessionPersistence.from_remembered(bool(_value_as_object(event)))
                        _render_login_actions.refresh()

                    if self._auth.bypass_enabled:
                        ui.switch(
                            "Show API pill on app lists",
                            value=login_show_api_actions,
                            on_change=_handle_login_show_api_actions_change,
                        ).props("color=accent")

                    with ui.column().classes("gap-1"):
                        ui.checkbox(
                            "Remember me",
                            value=False,
                            on_change=_handle_login_persistence_change,
                        ).props("color=accent")
                        ui.label("Keep this device signed in for 30 days. Leave this off on a shared device.").classes(
                            "text-sm mod-subtitle"
                        )

                    @ui.refreshable
                    def _render_login_actions() -> None:
                        with ui.row().classes(self._hero_action_row_classes()):
                            for action in self._login_actions(
                                next_path=next_path,
                                show_api_actions=login_show_api_actions,
                                persistence=login_persistence,
                            ):
                                self._action_link(ui=ui, label=action.label, url=action.url)

                    _render_login_actions()
                    self._render_login_information(ui=ui)
            if config.INDEV:
                self._render_login_dev_error_preview_card(ui=ui)

    def _render_login_information(self, *, ui: ModWebUi) -> None:
        with ui.element("div").classes("w-full border-t border-white/10 pt-4"):
            with ui.column().classes("w-full gap-3"):
                ui.label(
                    "Discord handles authentication; Yukibot only requests your identity and never "
                    "receives your password."
                ).classes("text-sm mod-subtitle")
                administrators = self._login_administrators()
                with ui.column().classes("gap-2"):
                    ui.label("Need access? Contact an administrator.").classes("mod-stat-label")
                    if administrators:
                        with ui.column().classes("gap-2"):
                            for level in self._login_administrator_levels():
                                level_administrators = tuple(
                                    administrator for administrator in administrators if administrator.level is level
                                )
                                with ui.element("div").classes(
                                    "grid grid-cols-1 sm:grid-cols-[4rem_minmax(0,1fr)] "
                                    "items-start gap-1 sm:gap-2 w-full"
                                ):
                                    ui.label(level.name.title()).classes("mod-stat-label sm:pt-1")
                                    if level_administrators:
                                        with ui.row().classes("gap-2 flex-wrap min-w-0 w-full"):
                                            for administrator in level_administrators:
                                                avatar_uri = self._login_administrator_avatar_uri(administrator)
                                                if avatar_uri is None:
                                                    self._badge(
                                                        ui=ui,
                                                        text=administrator.display_name,
                                                        tone="grey",
                                                        extra_classes=(
                                                            "max-w-full whitespace-normal break-words"
                                                        ),
                                                    )
                                                else:
                                                    self._badge_avatar(
                                                        ui=ui,
                                                        text=administrator.display_name,
                                                        tone="grey",
                                                        avatar_uri=avatar_uri,
                                                        extra_classes="max-w-full",
                                                    )
                                    else:
                                        ui.label("None listed").classes("text-sm mod-subtitle")
                    else:
                        ui.label("Administrator contacts are currently unavailable.").classes("text-sm mod-subtitle")
                with ui.row().classes("gap-2 flex-wrap"):
                    for action in self._login_information_actions():
                        self._action_link(
                            ui=ui,
                            label=action.label,
                            url=action.url,
                            compact=True,
                            new_tab=action.new_tab,
                        )

    def _login_administrators(self) -> tuple[_ModWebLoginAdministrator, ...]:
        if self._acl is None:
            return ()
        name_cache = config.Name_Cache()
        administrators = tuple(
            _ModWebLoginAdministrator(
                user_id=user_id,
                display_name=name_cache.web_display_name(user_id, f"Discord user {user_id}"),
                level=level,
                avatar_hash=name_cache.discord_avatar_hash(user_id),
            )
            for user_id, level in self._acl.explicit_roles().items()
            if level >= Power_Level.admin and user_id not in _LOGIN_CONTACT_EXCLUDED_USER_IDS
        )
        return tuple(
            sorted(
                administrators,
                key=lambda administrator: (
                    -int(administrator.level),
                    administrator.display_name.casefold(),
                    administrator.user_id,
                ),
            )
        )

    @staticmethod
    def _login_administrator_levels() -> tuple[Power_Level, ...]:
        return _LOGIN_ADMINISTRATOR_LEVELS

    @staticmethod
    def _login_administrator_avatar_uri(administrator: _ModWebLoginAdministrator) -> str | None:
        return mod_web_avatars._discord_avatar_uri(
            user_id=administrator.user_id,
            avatar_hash=administrator.avatar_hash,
        )

    @staticmethod
    def _login_information_actions() -> tuple[_ModWebLinkSpec, ...]:
        actions: list[_ModWebLinkSpec] = [
            _ModWebLinkSpec(label="About", url="/auth/about"),
            _ModWebLinkSpec(label="GitHub", url=_MOD_WEB_REPOSITORY_URL, new_tab=True),
        ]
        build_sha: str | None = config.MOD_WEB_BUILD_SHA
        if build_sha is not None:
            actions.append(
                _ModWebLinkSpec(
                    label=f"Build {build_sha[:7]}",
                    url=f"{_MOD_WEB_REPOSITORY_URL}/commit/{build_sha}",
                    new_tab=True,
                )
            )
        return tuple(actions)

    def _about_page_config(self) -> _ModWebStatusPageConfig:
        return _ModWebStatusPageConfig(
            title="About Yukibot",
            support_text="Discord, web, and dedicated game-server operations brought together in one place.",
            badge_text="About",
            badge_tone="black",
            accent_color_hex="#8b5cf6",
            icon_markup=self._about_icon_markup(),
            detail_label="Project",
            detail_text=("This rewrite was started by NaiTechie, also known as AiviA, and completed by APasz."),
            actions=(
                _ModWebLinkSpec(label="GitHub", url=_MOD_WEB_REPOSITORY_URL, new_tab=True),
                _ModWebLinkSpec(label="Home", url=self.index_path()),
            ),
        )

    @staticmethod
    def _about_supported_app_names() -> tuple[str, ...]:
        return tuple(scope.display_name for scope in config.AppScopes)

    def _render_about_page(self, *, ui: ModWebUi) -> None:
        self._apply_theme(ui=ui)
        with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
            self._render_status_page_panel(ui=ui, config=self._about_page_config())
            with ui.card().classes("mod-card w-full"):
                with ui.column().classes("gap-5 p-5"):
                    with ui.column().classes("gap-1"):
                        ui.label("What Yukibot does").classes("text-xl font-bold mod-title-small")
                        ui.label(
                            "Yukibot is a Discord bot and web dashboard built to operate a small number of "
                            "dedicated game servers. It combines live status, lifecycle controls, updates, "
                            "mods, configurations, saves, chat relay, and game-specific tools behind one "
                            "interface. Available features vary by game and server."
                        ).classes("text-sm mod-subtitle")
                    with ui.column().classes("gap-2"):
                        ui.label("Supported applications").classes("text-xl font-bold mod-title-small")
                        with ui.row().classes("gap-2 flex-wrap"):
                            for app_name in self._about_supported_app_names():
                                self._badge(ui=ui, text=app_name, tone="grey")
                    with ui.column().classes("gap-1"):
                        ui.label("How it fits together").classes("text-xl font-bold mod-title-small")
                        ui.label(
                            "This portal provides a shared dashboard for the Yuki and Erin bots' "
                            "own nodes. Identity is Discord backed while a separate ACL controls "
                            "what each account may view or change."
                        ).classes("text-sm mod-subtitle")

    def _render_login_dev_error_preview_card(self, *, ui: ModWebUi) -> None:
        with ui.card().classes("mod-card w-full"):
            with ui.column().classes("gap-4 p-5"):
                with ui.column().classes("gap-1"):
                    ui.label("Dev Previews").classes("text-xl font-bold mod-title-small")
                    ui.label("Test notification styles and error states without breaking a live app page.").classes(
                        "text-sm mod-subtitle"
                    )
                with ui.column().classes("gap-2"):
                    ui.label("Toast Previews").classes("mod-stat-label")

                    def _show_notification_preview(preview: _ModWebNotificationPreviewSpec) -> None:
                        timeout_milliseconds: int = (
                            _APP_ACTION_NOTIFICATION_TIMEOUT_MILLISECONDS
                            if preview.timeout_milliseconds is None
                            else preview.timeout_milliseconds
                        )
                        for _ in range(preview.repeat_count):
                            ui.notify(
                                preview.message,
                                close_button=preview.close_button,
                                multi_line=preview.multi_line,
                                type=preview.notification_type,
                                timeout=timeout_milliseconds,
                            )

                    def _notification_preview_handler(
                        preview: _ModWebNotificationPreviewSpec,
                    ) -> Callable[[object | None], None]:
                        def _handle_notification_preview(_: object | None = None) -> None:
                            _show_notification_preview(preview)

                        return _handle_notification_preview

                    with ui.row().classes("gap-2 flex-wrap"):
                        for preview in self._dev_notification_preview_actions():
                            ui.button(
                                preview.label,
                                on_click=_notification_preview_handler(preview),
                            ).classes("mod-list-button")
                ui.label("Error Page Previews").classes("mod-stat-label")
                with ui.row().classes("gap-2 flex-wrap"):
                    for action in self._dev_error_preview_actions():
                        self._action_link(
                            ui=ui,
                            label=action.label,
                            url=action.url,
                            compact=True,
                            new_tab=action.new_tab,
                        )

    @staticmethod
    def _dev_notification_preview_actions() -> tuple[_ModWebNotificationPreviewSpec, ...]:
        return (
            _ModWebNotificationPreviewSpec(
                label="Positive Toast",
                message="Positive notification preview.",
                notification_type="positive",
            ),
            _ModWebNotificationPreviewSpec(
                label="Negative Toast",
                message="Negative notification preview.",
                notification_type="negative",
            ),
            _ModWebNotificationPreviewSpec(
                label="Warning Toast",
                message="Warning notification preview.",
                notification_type="warning",
            ),
            _ModWebNotificationPreviewSpec(
                label="Info Toast",
                message="Info notification preview.",
                notification_type="info",
            ),
            _ModWebNotificationPreviewSpec(
                label="Ongoing Toast",
                message="Ongoing notification preview.",
                notification_type="ongoing",
            ),
            _ModWebNotificationPreviewSpec(
                label="Grouped Duplicate",
                message="Intentional grouped duplicate preview.",
                notification_type="info",
                repeat_count=2,
            ),
            _ModWebNotificationPreviewSpec(
                label="Long Multiline",
                message=(
                    "Long multiline notification preview for checking wrapping, spacing, and readability "
                    "when a toast contains more detail than usual."
                ),
                notification_type="warning",
                multi_line=True,
            ),
            _ModWebNotificationPreviewSpec(
                label="Persistent Dismissible",
                message="Persistent notification preview; dismiss it with the button.",
                notification_type="ongoing",
                close_button="Dismiss",
                timeout_milliseconds=0,
            ),
        )

    def _login_page_subtitle(self) -> str:
        if self._auth.bypass_enabled:
            return "Choose a dev sign-in level to test mod web permissions."
        return "Sign in with Discord to use mod web actions."

    def _login_actions(
        self,
        *,
        next_path: str,
        show_api_actions: bool,
        persistence: ModWebSessionPersistence = ModWebSessionPersistence.BROWSER_SESSION,
    ) -> tuple[_ModWebLinkSpec, ...]:
        login_next_path: str = self._app_list_view_url(next_path, show_api_actions=show_api_actions)
        remember: str = str(persistence is ModWebSessionPersistence.REMEMBERED).lower()
        if self._auth.bypass_enabled:
            return tuple[_ModWebLinkSpec, ...](
                _ModWebLinkSpec(
                    label=f"Sign in {level.name.title()}",
                    url="/auth/dev-login?"
                    + urlencode({"level": level.name, "next_path": login_next_path, "remember": remember}),
                )
                for level in self._auth.bypass_levels
            )
        return (
            _ModWebLinkSpec(
                label="Sign in with Discord",
                url=f"/auth/login?{urlencode({'next_path': login_next_path, 'remember': remember})}",
            ),
        )

    @staticmethod
    def _dev_error_preview_actions() -> tuple[_ModWebLinkSpec, ...]:
        return (
            _ModWebLinkSpec(label="Access Denied", url="/mod-web/dev/error/access-denied"),
            _ModWebLinkSpec(label="Sign-in Unavailable", url="/mod-web/dev/error/sign-in-unavailable"),
            _ModWebLinkSpec(label="OAuth Failure", url="/mod-web/dev/error/oauth-failure"),
            _ModWebLinkSpec(label="Page Unavailable", url="/mod-web/dev/error/page-unavailable"),
            _ModWebLinkSpec(label="Chat Unavailable", url="/mod-web/dev/error/chat-unavailable"),
            _ModWebLinkSpec(label="Node Unavailable", url="/mod-web/dev/error/node-unavailable"),
            _ModWebLinkSpec(label="Remote JSON Invalid", url="/mod-web/dev/error/remote-json-invalid"),
            _ModWebLinkSpec(label="Remote Timeout", url="/mod-web/dev/error/remote-timeout"),
            _ModWebLinkSpec(label="Remote Rejected", url="/mod-web/dev/error/remote-rejected"),
            _ModWebLinkSpec(label="Redirect Loop 310", url="/mod-web/dev/error/redirect-loop"),
            _ModWebLinkSpec(label="Framework 404", url="/mod-web/dev/error/framework-404"),
            _ModWebLinkSpec(label="Framework 500", url="/mod-web/dev/error/framework-500"),
            _ModWebLinkSpec(label="NiceGUI Exception", url="/mod-web/dev/error/nicegui-exception"),
            _ModWebLinkSpec(label="Refresh Shutdown", url="/mod-web/dev/error/refresh-shutdown"),
            _ModWebLinkSpec(label="Config Fail Toasts", url="/mod-web/dev/error/config-failure"),
            _ModWebLinkSpec(label="Chat Stream WS", url="/mod-web/dev/error/chat-stream-websocket"),
        )

    def _render_forbidden_page(self, *, ui: ModWebUi, user: ModWebUser, required_level: Power_Level) -> None:
        current_level: Power_Level = self._acl.level_of(user.discord_id) if self._acl is not None else Power_Level.guest
        self._apply_theme(ui=ui)
        with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
            self._render_user_header(ui=ui, user=user)
            self._render_status_page_panel(
                ui=ui,
                config=_ModWebStatusPageConfig(
                    title="Access denied",
                    support_text="Your current sign-in level does not allow access to this page.",
                    badge_text="Restricted",
                    badge_tone="warn",
                    accent_color_hex="#f59e0b",
                    icon_markup=self._access_denied_icon_markup(),
                    detail_text=f"{current_level.name.title()} access is below {required_level.name.title()}.",
                    detail_label="Required Access",
                    actions=(_ModWebLinkSpec(label="Home", url=self.index_path()),),
                ),
            )

    def _render_user_header(self, *, ui: ModWebUi, user: ModWebUser) -> None:
        display_name: str = self._web_display_name(user)
        avatar_uri: str = self._user_avatar_uri(user)
        with ui.row().classes("w-full min-w-0 items-start gap-4 flex-wrap lg:flex-nowrap mod-user-header-row"):
            with ui.element("div").classes("shrink-0 mod-user-header-surface").style(self._user_header_surface_style()):
                with ui.column().classes("w-full h-full justify-between gap-2"):
                    with ui.row().classes("items-center gap-2 no-wrap min-w-0"):
                        ui.html(self._user_avatar_markup(avatar_uri=avatar_uri, display_name=display_name))
                        with ui.column().classes("gap-1 min-w-0"):
                            ui.label(f"{display_name}").classes("text-sm text-white break-all leading-none")
                            self._badge(ui=ui, text=self._user_level_label(user), tone=self._user_level_tone(user))
                    with ui.row().classes("w-full items-stretch gap-2"):
                        self._render_user_home_button(ui=ui, user=user)
                        self._render_user_utility_launcher(ui=ui, user=user)
            with ui.element("div").classes("min-w-0 grow w-full mod-user-header-tray-shell").style(self._user_header_tray_style()):
                self._render_user_notification_tray(ui=ui, user=user)

    def _render_user_home_button(self, *, ui: ModWebUi, user: ModWebUser) -> None:
        def _handle_home_click(_: object | None = None) -> None:
            self._navigate_home(ui=ui, user=user)

        ui.button("", on_click=_handle_home_click).props(
            "icon=home flat aria-label=Home"
        ).classes(
            f"{_USER_HEADER_ICON_BUTTON_CLASSES} mod-user-home-button"
        )

    def _navigate_home(self, *, ui: ModWebUi, user: ModWebUser) -> bool:
        active_upload: bool = any(
            item.kind is ModWebNotificationTrayItemKind.UPLOAD
            and item.state is ModWebNotificationTrayItemState.ACTIVE
            for item in self._backend.user_transfer_items(user_id=user.discord_id)
        )
        if active_upload:
            ui.notify(
                "An upload is still in progress. Stay on this page until it completes or fails.",
                type="warning",
            )
            return False
        ui.navigate.to(self.index_path())
        return True

    def _alias_known_scopes(self) -> tuple[str, ...]:
        scopes: set[str] = {scope.value for scope in config.AppScopes}
        manager = self._manager
        if manager is None:
            return tuple(sorted(scopes, key=str.casefold))
        list_known_scopes = getattr(manager, "list_known_scopes", None)
        if callable(list_known_scopes):
            known_scopes = list_known_scopes()
            if isinstance(known_scopes, tuple) and all(isinstance(scope, str) for scope in known_scopes):
                scopes.update(scope.strip().lower() for scope in known_scopes if scope.strip())
                return tuple(sorted(scopes, key=str.casefold))
        apps = getattr(manager, "apps", None)
        if isinstance(apps, dict):
            scopes.update(app.scope.strip().lower() for app in apps.values() if app.scope.strip())
        list_create_scopes = getattr(manager, "list_create_scopes", None)
        if callable(list_create_scopes):
            create_scopes = list_create_scopes()
            if isinstance(create_scopes, tuple) and all(isinstance(scope, str) for scope in create_scopes):
                scopes.update(scope.strip().lower() for scope in create_scopes if scope.strip())
        return tuple(sorted(scopes, key=str.casefold))

    @staticmethod
    def _alias_target_label(*, name_cache: config.Name_Cache, user_id: int, viewer: ModWebUser) -> str:
        entry = name_cache.by_id.get(user_id)
        username = entry.account if entry is not None else (viewer.username if user_id == viewer.discord_id else None)
        global_name = entry.global_name if entry is not None else (viewer.global_name if user_id == viewer.discord_id else None)
        primary = global_name or username
        if primary is None:
            return str(user_id)
        return f"{primary} ({user_id})"

    def _alias_target_options(self, *, name_cache: config.Name_Cache, viewer: ModWebUser) -> dict[str, str]:
        user_ids: set[int] = {viewer.discord_id}
        user_ids.update(user_id for user_id in name_cache.by_id if isinstance(user_id, int))
        ordered_user_ids = sorted(
            user_ids,
            key=lambda user_id: self._alias_target_label(name_cache=name_cache, user_id=user_id, viewer=viewer).casefold(),
        )
        return {
            str(user_id): self._alias_target_label(name_cache=name_cache, user_id=user_id, viewer=viewer)
            for user_id in ordered_user_ids
        }

    def _build_alias_panel(self, *, ui: ModWebUi, user: ModWebUser) -> Callable[[], None]:
        can_switch_user = self._user_has_level(user, Power_Level.sudo)
        alias_dialog = None
        alias_dialog_rendered = False

        def _show_alias_panel() -> None:
            nonlocal alias_dialog, alias_dialog_rendered
            if alias_dialog_rendered:
                if alias_dialog is None:
                    raise RuntimeError("Alias dialog was not rendered.")
                alias_dialog.open()
                return

            alias_dialog_rendered = True
            name_cache = config.Name_Cache()
            state: dict[str, int] = {"target_user_id": user.discord_id}
            drafts_by_user: dict[int, _AliasDialogDraft] = {}
            form_controls: dict[str, ModWebValueContainer] = {}
            app_alias_controls: dict[str, ModWebValueContainer] = {}
            refresh_alias_body: Callable[[], None] = lambda: None

            def _target_user_id() -> int:
                target_user_id = state.get("target_user_id", user.discord_id)
                if not can_switch_user:
                    return user.discord_id
                return target_user_id

            def _current_app_scopes() -> tuple[str, ...]:
                return self._alias_known_scopes()

            def _build_alias_draft(target_user_id: int) -> _AliasDialogDraft:
                return _AliasDialogDraft(
                    display_name=name_cache.get_display_override(target_user_id) or "",
                    app_aliases={
                        scope: name_cache.get_game_alias(target_user_id, scope) or "" for scope in _current_app_scopes()
                    },
                    steam_id=name_cache.get_platform_id(target_user_id, "steam") or "",
                    minecraft_uuid=name_cache.get_game_uuid(target_user_id, "minecraft") or "",
                )

            def _draft_for_user(target_user_id: int) -> _AliasDialogDraft:
                draft = drafts_by_user.get(target_user_id)
                if draft is None:
                    draft = _build_alias_draft(target_user_id)
                    drafts_by_user[target_user_id] = draft
                return draft

            def _set_control_value(control: object, value: str) -> None:
                setattr(control, "value", value)

            def _capture_alias_draft() -> None:
                if not form_controls:
                    return
                draft = _draft_for_user(_target_user_id())
                draft.display_name = _value_as_text(form_controls["display_name"])
                draft.add_alias = _value_as_text(form_controls["add_alias"])
                draft.steam_id = _value_as_text(form_controls["steam_id"])
                draft.minecraft_uuid = _value_as_text(form_controls["minecraft_uuid"])
                draft.app_aliases = {
                    scope: _value_as_text(control)
                    for scope, control in app_alias_controls.items()
                }

            def _save_alias_form() -> None:
                _capture_alias_draft()
                target_user_id = _target_user_id()
                draft = _draft_for_user(target_user_id)
                changed_fields: list[str] = []
                try:
                    if name_cache.set_display_override(target_user_id, draft.display_name):
                        changed_fields.append("display name")
                    for scope in _current_app_scopes():
                        next_alias = draft.app_aliases.get(scope, "")
                        current_alias = name_cache.get_game_alias(target_user_id, scope) or ""
                        if next_alias:
                            if next_alias != current_alias:
                                name_cache.set_game_alias(target_user_id, scope, next_alias)
                                changed_fields.append(f"{scope.title()} alias")
                            continue
                        if current_alias:
                            name_cache.remove_game_alias(target_user_id, scope)
                            changed_fields.append(f"{scope.title()} alias")
                    if name_cache.set_platform_id(target_user_id, "steam", draft.steam_id):
                        changed_fields.append("Steam ID")
                    if name_cache.set_game_uuid(target_user_id, "minecraft", draft.minecraft_uuid):
                        changed_fields.append("Minecraft UUID")
                except ValueError as xcp:
                    ui.notify(str(xcp), type="negative")
                    return
                drafts_by_user[target_user_id] = _build_alias_draft(target_user_id)
                ui.notify("Saved alias changes." if changed_fields else "Alias values are unchanged.", type="positive")
                refresh_alias_body()

            def _handle_target_user_change(event: ModWebValueContainer) -> None:
                if not can_switch_user:
                    return
                raw_value = str(_value_as_object(event) or "").strip()
                if not raw_value.isdigit():
                    ui.notify("Selected user is invalid.", type="negative")
                    return
                next_user_id = int(raw_value)
                if next_user_id == state["target_user_id"]:
                    return
                _capture_alias_draft()
                state["target_user_id"] = next_user_id
                refresh_alias_body()

            with ui.dialog() as alias_dialog:
                with ui.card().classes("mod-card mod-dialog-card mod-app-details-dialog-card"):
                    with ui.column().classes("w-full gap-4 mod-app-details-layout"):
                        with ui.column().classes("gap-1"):
                            ui.label("Alias").classes("text-xl font-black mod-title-small")
                            ui.label("Manage display names, aliases, game handles, and linked identities.").classes(
                                "mod-subtitle text-sm"
                            )
                        target_select = (
                            ui.select(
                                self._alias_target_options(name_cache=name_cache, viewer=user),
                                value=str(state["target_user_id"]),
                                label="User",
                                on_change=_handle_target_user_change,
                            )
                            .props("filled square dense hide-bottom-space color=accent options-dark")
                            .classes("mod-app-details-field w-full")
                        )
                        if not can_switch_user:
                            target_select.disable()

                        def _render_inline_alias_input(
                            *,
                            label: str,
                            value: str,
                            clear_tooltip: str,
                            clear_icon: str = "backspace",
                            control_key: str,
                        ) -> None:
                            with ui.element("div").classes(
                                "grid grid-cols-[minmax(0,1fr)_3rem] items-stretch gap-2 w-full min-w-0"
                            ):
                                control = (
                                    ui.input(label, value=value)
                                    .props("filled square dense hide-bottom-space color=accent")
                                    .classes("mod-app-details-field min-w-0")
                                )
                                form_controls[control_key] = control
                                def on_clear() -> None:
                                    _set_control_value(control, "")
                                    _capture_alias_draft()

                                clear_button = ui.button("", on_click=lambda _: on_clear()).props(
                                    f'icon={clear_icon} flat dense round aria-label="{clear_tooltip}"'
                                ).classes("mod-list-button secondary w-full min-w-0 px-2 py-2")
                                self._attach_text_tooltip(ui=ui, target=clear_button, text=clear_tooltip)

                        def _render_alias_body() -> None:
                            target_user_id = _target_user_id()
                            form_controls.clear()
                            app_alias_controls.clear()
                            target_names = name_cache.by_id.get(target_user_id, config.UserNames())
                            draft = _draft_for_user(target_user_id)
                            current_aliases = tuple(sorted(target_names.nicknames, key=str.casefold))
                            current_app_scopes = _current_app_scopes()
                            with ui.column().classes("w-full gap-4"):
                                with ui.column().classes("mod-app-details-section gap-3"):
                                    ui.label("Display Name").classes("mod-stat-label")
                                    _render_inline_alias_input(
                                        label="Display Name",
                                        value=draft.display_name,
                                        clear_tooltip="Clear display name",
                                        control_key="display_name",
                                    )

                                with ui.column().classes("mod-app-details-section gap-3"):
                                    ui.label("General Aliases").classes("mod-stat-label")
                                    def _add_general_alias() -> None:
                                        _capture_alias_draft()
                                        draft = _draft_for_user(target_user_id)
                                        value = draft.add_alias
                                        draft.add_alias = ""
                                        self._add_alias_general_name(
                                            ui=ui,
                                            name_cache=name_cache,
                                            user_id=target_user_id,
                                            value=value,
                                            refresh=refresh_alias_body,
                                        )

                                    with ui.element("div").classes(
                                        "grid grid-cols-[minmax(0,1fr)_3rem_3rem] items-stretch gap-2 w-full min-w-0"
                                    ):
                                        add_alias_control = (
                                            ui.input("Add General Alias", value=draft.add_alias)
                                            .props("filled square dense hide-bottom-space color=accent")
                                            .classes("mod-app-details-field min-w-0")
                                        )
                                        form_controls["add_alias"] = add_alias_control
                                        reset_alias_button = ui.button(
                                            "",
                                            on_click=lambda _:
                                            (
                                                _set_control_value(add_alias_control, ""),
                                                _capture_alias_draft(),
                                            ),
                                        ).props('icon=restart_alt flat dense round aria-label="Reset alias input"').classes(
                                            "mod-list-button secondary w-full min-w-0 px-2 py-2"
                                        )
                                        add_alias_button = ui.button(
                                            "",
                                            on_click=lambda _: _add_general_alias(),
                                        ).props('icon=add flat dense round aria-label="Add general alias"').classes(
                                            "mod-list-button w-full min-w-0 px-2 py-2"
                                        )
                                        self._attach_text_tooltip(
                                            ui=ui,
                                            target=reset_alias_button,
                                            text="Reset alias input",
                                        )
                                        self._attach_text_tooltip(
                                            ui=ui,
                                            target=add_alias_button,
                                            text="Add general alias",
                                        )
                                    if current_aliases:
                                        for alias in current_aliases:
                                            with ui.row().classes("w-full items-center justify-between gap-2"):
                                                ui.label(alias).classes("mod-subtitle text-sm break-all")
                                                remove_button = ui.button(
                                                    "",
                                                    on_click=lambda _, alias=alias:
                                                    (
                                                        _capture_alias_draft(),
                                                        self._remove_alias_general_name(
                                                            ui=ui,
                                                            name_cache=name_cache,
                                                            user_id=target_user_id,
                                                            alias=alias,
                                                            refresh=refresh_alias_body,
                                                        ),
                                                    ),
                                                ).props('icon=delete flat dense round aria-label="Remove alias"').classes(
                                                    "mod-list-button secondary shrink-0 px-2 py-2"
                                                )
                                                self._attach_text_tooltip(ui=ui, target=remove_button, text="Remove alias")
                                    else:
                                        ui.label("No general aliases set.").classes("mod-subtitle text-xs")

                                with ui.column().classes("mod-app-details-section gap-3"):
                                    ui.label("App Aliases").classes("mod-stat-label")
                                    if current_app_scopes:
                                        for scope in current_app_scopes:
                                            _render_inline_alias_input(
                                                label=f"{scope.title()} Alias",
                                                value=draft.app_aliases.get(scope, ""),
                                                clear_tooltip=f"Clear {scope.title()} alias",
                                                control_key=f"app_alias:{scope}",
                                            )
                                            app_alias_controls[scope] = form_controls[f"app_alias:{scope}"]
                                    else:
                                        ui.label("No app scopes are available on this node.").classes(
                                            "mod-subtitle text-xs"
                                        )

                                with ui.column().classes("mod-app-details-section gap-3"):
                                    ui.label("Linked Accounts").classes("mod-stat-label")
                                    _render_inline_alias_input(
                                        label="Steam ID",
                                        value=draft.steam_id,
                                        clear_tooltip="Clear Steam ID",
                                        control_key="steam_id",
                                    )
                                    _render_inline_alias_input(
                                        label="Minecraft UUID",
                                        value=draft.minecraft_uuid,
                                        clear_tooltip="Clear Minecraft UUID",
                                        control_key="minecraft_uuid",
                                    )

                        refreshable = getattr(ui, "refreshable", None)
                        if callable(refreshable):
                            render_alias_body = ui.refreshable(_render_alias_body)
                            refresh_alias_body = render_alias_body.refresh
                        else:
                            render_alias_body = _render_alias_body
                            refresh_alias_body = _render_alias_body

                        render_alias_body()
                        with ui.row().classes("w-full justify-end gap-2 mod-app-details-actions"):
                            ui.button("Save", on_click=lambda _: _save_alias_form()).classes("mod-list-button")
                            ui.button("Close", on_click=alias_dialog.close).classes("mod-list-button secondary")

            alias_dialog.open()

        return _show_alias_panel

    @staticmethod
    def _save_alias_display_override(
        *,
        ui: ModWebUi,
        name_cache: config.Name_Cache,
        user_id: int,
        value: str | None,
        refresh: Callable[[], None],
    ) -> None:
        try:
            changed = name_cache.set_display_override(user_id, value)
        except ValueError as xcp:
            ui.notify(str(xcp), type="negative")
            return
        ui.notify("Saved display name." if changed else "Display name is unchanged.", type="positive")
        refresh()

    @staticmethod
    def _add_alias_general_name(
        *,
        ui: ModWebUi,
        name_cache: config.Name_Cache,
        user_id: int,
        value: str,
        refresh: Callable[[], None],
    ) -> None:
        try:
            name_cache.add_name(user_id, value, False)
        except ValueError as xcp:
            ui.notify(str(xcp), type="negative")
            return
        ui.notify("Added general alias.", type="positive")
        refresh()

    @staticmethod
    def _remove_alias_general_name(
        *,
        ui: ModWebUi,
        name_cache: config.Name_Cache,
        user_id: int,
        alias: str,
        refresh: Callable[[], None],
    ) -> None:
        name_cache.remove_name(user_id, alias)
        ui.notify(f"Removed `{alias}`.", type="positive")
        refresh()

    @staticmethod
    def _save_alias_game_name(
        *,
        ui: ModWebUi,
        name_cache: config.Name_Cache,
        user_id: int,
        scope: str,
        value: str,
        refresh: Callable[[], None],
    ) -> None:
        try:
            name_cache.set_game_alias(user_id, scope, value)
        except ValueError as xcp:
            ui.notify(str(xcp), type="negative")
            return
        ui.notify(f"Saved {scope.title()} alias.", type="positive")
        refresh()

    @staticmethod
    def _clear_alias_game_name(
        *,
        ui: ModWebUi,
        name_cache: config.Name_Cache,
        user_id: int,
        scope: str,
        refresh: Callable[[], None],
    ) -> None:
        name_cache.remove_game_alias(user_id, scope)
        ui.notify(f"Cleared {scope.title()} alias.", type="positive")
        refresh()

    @staticmethod
    def _save_alias_platform_id(
        *,
        ui: ModWebUi,
        name_cache: config.Name_Cache,
        user_id: int,
        platform: str,
        value: str | None,
        refresh: Callable[[], None],
    ) -> None:
        try:
            changed = name_cache.set_platform_id(user_id, platform, value)
        except ValueError as xcp:
            ui.notify(str(xcp), type="negative")
            return
        ui.notify(
            f"Saved {platform.title()} identity." if changed else f"{platform.title()} identity is unchanged.",
            type="positive",
        )
        refresh()

    @staticmethod
    def _save_alias_game_uuid(
        *,
        ui: ModWebUi,
        name_cache: config.Name_Cache,
        user_id: int,
        scope: str,
        value: str | None,
        refresh: Callable[[], None],
    ) -> None:
        try:
            changed = name_cache.set_game_uuid(user_id, scope, value)
        except ValueError as xcp:
            ui.notify(str(xcp), type="negative")
            return
        ui.notify(
            f"Saved {scope.title()} UUID." if changed else f"{scope.title()} UUID is unchanged.",
            type="positive",
        )
        refresh()

    def _render_user_utility_launcher(self, *, ui: ModWebUi, user: ModWebUser) -> None:
        open_alias_panel = self._build_alias_panel(ui=ui, user=user)
        open_discord_settings = (
            self._build_discord_settings_panel(ui=ui, user=user) if self._user_can_manage_discord_settings(user) else None
        )
        open_fake_chat_preview = (
            self._build_fake_chat_preview_panel(ui=ui) if self._user_can_use_fake_chat_preview(user) else None
        )

        def _simulate(kind: ModWebNotificationTrayItemKind) -> None:
            current_count: int = len(self._backend.user_transfer_items(user_id=user.discord_id))
            filename = (
                f"sim-upload-{current_count + 1:02d}.jar"
                if kind is ModWebNotificationTrayItemKind.UPLOAD
                else f"sim-download-{current_count + 1:02d}.zip"
            )
            detail_text = (
                "Simulated upload transfer."
                if kind is ModWebNotificationTrayItemKind.UPLOAD
                else "Simulated download transfer."
            )
            try:
                self._backend.start_simulated_transfer(
                    user_id=user.discord_id,
                    kind=kind,
                    filename=filename,
                    detail_text=detail_text,
                    duration_seconds=6.0,
                    node_color_hex=self._primary_guild_bot_role_color_hex(),
                    app_color_hex=None,
                )
            except RuntimeError as xcp:
                ui.notify(str(xcp), type="warning")

        def _clear_transfers() -> None:
            self._backend.clear_user_transfers(user_id=user.discord_id)

        action_specs: list[tuple[str, Callable[[], object]]] = []
        if config.INDEV:
            action_specs.extend(
                (
                    ("Sim Upload", lambda: _simulate(ModWebNotificationTrayItemKind.UPLOAD)),
                    ("Sim Download", lambda: _simulate(ModWebNotificationTrayItemKind.DOWNLOAD)),
                    ("Clear Transfers", _clear_transfers),
                )
            )
        action_specs.append(("Alias", open_alias_panel))
        if open_discord_settings is not None:
            action_specs.append(("Discord", open_discord_settings))
        if open_fake_chat_preview is not None:
            action_specs.append(("Fake Chat", open_fake_chat_preview))
        action_specs.append(("Log out", lambda: ui.navigate.to("/auth/logout")))

        menu_factory = getattr(ui, "menu", None)
        if callable(menu_factory):
            create_menu = cast("Callable[[], Element]", menu_factory)
            with ui.button("").props("icon=menu flat aria-label=Utilities").classes(
                f"{_USER_HEADER_ICON_BUTTON_CLASSES} mod-user-menu-button"
            ):
                with create_menu().classes("mod-chat-entry-menu min-w-[12rem]"):
                    for label, action in action_specs:
                        ui.menu_item(label, on_click=lambda _, action=action: action()).classes("mod-chat-entry-menu-item")
            return

        with ui.dialog() as utility_dialog:
            with ui.card().classes("mod-card mod-dialog-card"):
                with ui.column().classes("w-full gap-2 p-4"):
                    ui.label("Tray Tools").classes("text-lg font-black mod-title-small")
                    for label, action in action_specs:
                        ui.button(label, on_click=lambda _, action=action: (action(), utility_dialog.close())).classes(
                            "mod-list-button secondary w-full"
                        )

        ui.button("", on_click=utility_dialog.open).props("icon=menu flat aria-label=Utilities").classes(
            f"{_USER_HEADER_ICON_BUTTON_CLASSES} mod-user-menu-button"
        )

    def _render_user_notification_tray(self, *, ui: ModWebUi, user: ModWebUser) -> None:
        def _render_items(items: tuple[_ModWebNotificationTrayItem, ...]) -> None:
            if not items:
                return
            with ui.row().classes("w-full items-stretch justify-start content-start gap-2 flex-wrap min-w-0 mod-user-header-tray"):
                for item in items:
                    card_classes: str = "shrink-0 overflow-hidden mod-transfer-card"
                    if item.blink:
                        card_classes = f"{card_classes} animate-pulse"
                    with ui.element("div").classes(card_classes).style(self._notification_tray_card_style(item)):
                        with ui.row().classes("w-full h-full items-stretch no-wrap gap-0"):
                            with ui.element("div").classes("shrink-0 flex items-center justify-center").style(
                                self._notification_tray_badge_style(item)
                            ):
                                ui.label(self._notification_tray_item_glyph(item)).classes(
                                    "text-[0.62rem] font-black tracking-[0.2em] text-white"
                                ).style("writing-mode: vertical-rl; transform: rotate(180deg);")
                            ui.element("div").classes("shrink-0").style(self._notification_tray_node_stripe_style(item))
                            with ui.column().classes("min-w-0 grow gap-0").style("background: #000000;"):
                                with ui.column().classes("min-w-0 grow justify-center gap-1 px-3 pt-2 pb-2"):
                                    ui.label(item.label).classes("text-base font-semibold leading-tight text-white").style(
                                        "display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;"
                                        " overflow: hidden; word-break: break-word;"
                                    )
                                    ui.label(item.detail_text or item.state.name.title()).classes(
                                        "text-xs leading-tight text-white/80"
                                    ).style(
                                        "display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical;"
                                        " overflow: hidden;"
                                    )
                                with ui.element("div").classes("relative w-full overflow-hidden").style(
                                    self._notification_tray_progress_track_style(item)
                                ):
                                    if item.progress_percent is not None:
                                        ui.element("div").style(self._notification_tray_progress_fill_style(item))

        refreshable = getattr(ui, "refreshable", None)
        if not callable(refreshable):
            _render_items(self._user_notification_tray_items(user=user))
            return

        @ui.refreshable
        def _tray() -> None:
            with ui.element("div").classes("w-full min-w-0 mod-user-header-tray-block").style(self._user_header_tray_block_style()):
                _render_items(self._user_notification_tray_items(user=user))

        _tray()
        ui_context: object | None = getattr(cast(object, ui), "context", None)
        client: object | None = getattr(ui_context, "client", None) if ui_context is not None else None
        safe_invoke = getattr(client, "safe_invoke", None) if client is not None else None
        if not callable(safe_invoke):
            return
        loop = asyncio.get_running_loop()

        def _request_tray_refresh() -> None:
            def _queue_refresh() -> None:
                safe_invoke(_tray.refresh)

            loop.call_soon_threadsafe(_queue_refresh)

        unsubscribe = self._backend.subscribe_user_transfers(
            user_id=user.discord_id,
            subscriber=_request_tray_refresh,
        )
        self._register_client_cleanup(ui=ui, cleanup=unsubscribe)

    def _user_notification_tray_items(self, *, user: ModWebUser) -> tuple[_ModWebNotificationTrayItem, ...]:
        return self._backend.user_transfer_items(user_id=user.discord_id)

    @staticmethod
    def _notification_tray_item_glyph(item: _ModWebNotificationTrayItem) -> str:
        if item.state is ModWebNotificationTrayItemState.ERROR:
            return "ERROR"
        if item.state is ModWebNotificationTrayItemState.SUCCESS:
            return "DONE"
        if item.kind is ModWebNotificationTrayItemKind.UPLOAD:
            return "UPLOAD"
        if item.kind is ModWebNotificationTrayItemKind.DOWNLOAD:
            return "DOWNLOAD"
        return "NOTICE"

    @staticmethod
    def _notification_tray_card_style(item: _ModWebNotificationTrayItem) -> str:
        border_color = item.app_color_hex or "#000000"
        return (
            f"height: {_USER_HEADER_TRAY_CARD_HEIGHT_REM:.2f}rem;"
            f"width: calc({_USER_HEADER_TRAY_CARD_HEIGHT_REM:.2f}rem * 4);"
            f"max-width: calc({_USER_HEADER_TRAY_CARD_HEIGHT_REM:.2f}rem * 4);"
            f"aspect-ratio: {_USER_HEADER_SURFACE_ASPECT_RATIO};"
            f"border: 2px solid {border_color}; background: #000000;"
            " box-shadow: 0 0 0 1px rgba(255,255,255,0.05);"
        )

    @staticmethod
    def _notification_tray_badge_style(item: _ModWebNotificationTrayItem) -> str:
        return (
            "width: 2.1rem; min-height: 100%; padding: 0.2rem 0.1rem;"
            f" background: {ModWebStatusMixin._notification_tray_badge_background(item)};"
        )

    @staticmethod
    def _notification_tray_node_stripe_style(item: _ModWebNotificationTrayItem) -> str:
        node_color = item.node_color_hex or "#000000"
        return f"width: 0.2rem; align-self: stretch; background: {node_color};"

    @staticmethod
    def _notification_tray_progress_track_style(item: _ModWebNotificationTrayItem) -> str:
        app_border_color = item.app_color_hex or "#000000"
        return f"height: 0.9rem; background: #42106c; border-top: 1px solid {app_border_color};"

    @staticmethod
    def _user_header_surface_style() -> str:
        return (
            f"min-height: {_USER_HEADER_SURFACE_MIN_HEIGHT_REM:.2f}rem;"
            f"width: {_USER_HEADER_STACK_WIDTH_REM:.2f}rem;"
            f"min-width: {_USER_HEADER_STACK_WIDTH_REM:.2f}rem;"
            "display: flex; flex-direction: column; justify-content: space-between;"
            "padding: 0.5rem 0.75rem;"
            "box-sizing: border-box;"
            "border: 1px solid rgba(255,255,255,0.08);"
            "background: rgba(0,0,0,0.28);"
            "overflow: hidden;"
        )

    @staticmethod
    def _user_header_tray_style() -> str:
        return (
            f"min-height: {_USER_HEADER_SURFACE_MIN_HEIGHT_REM:.2f}rem;"
            "display: flex;"
            "align-items: flex-start;"
            "justify-content: flex-start;"
        )

    @staticmethod
    def _user_header_tray_block_style() -> str:
        return "width: 100%;"

    @staticmethod
    def _notification_tray_badge_background(item: _ModWebNotificationTrayItem) -> str:
        if item.state is ModWebNotificationTrayItemState.ERROR:
            return "#b91c1c"
        if item.state is ModWebNotificationTrayItemState.WARNING:
            return "#d97706"
        if item.state is ModWebNotificationTrayItemState.SUCCESS:
            return "#15803d"
        if item.kind is ModWebNotificationTrayItemKind.DOWNLOAD:
            return "#ea580c"
        if item.kind is ModWebNotificationTrayItemKind.UPLOAD:
            return "#0f766e"
        return "#475569"

    @staticmethod
    def _notification_tray_progress_fill_style(item: _ModWebNotificationTrayItem) -> str:
        width_percent: float = item.progress_percent or 0.0
        if item.kind is ModWebNotificationTrayItemKind.UPLOAD:
            return (
                f"position: absolute; top: 0; right: 0; height: 100%; width: {width_percent:.2f}%;"
                " background: linear-gradient(270deg, #7c3aed, #4c1d95);"
            )
        return (
            f"position: absolute; top: 0; left: 0; height: 100%; width: {width_percent:.2f}%;"
            " background: linear-gradient(90deg, #7c3aed, #4c1d95);"
        )

    def _user_can_use_fake_chat_preview(self, user: ModWebUser) -> bool:
        return self._user_has_level(user, Power_Level.root)

    def _user_can_manage_discord_settings(self, user: ModWebUser) -> bool:
        return self._manager is not None and self._user_has_level(user, Power_Level.sudo)

    def _build_discord_settings_panel(self, *, ui: ModWebUi, user: ModWebUser) -> Callable[[], None]:
        current_settings = self._node_api.read_discord_settings()
        activity_settings = current_settings.activity
        available_fields_text = config.format_discord_activity_fields(config.DiscordActivityField)
        can_edit_refresh_rate = self._user_has_level(user, Power_Level.root)

        def _show_discord_settings_panel() -> None:
            discord_settings_overlay.style(remove="display: none;")

        def _hide_discord_settings_panel() -> None:
            discord_settings_overlay.style(add="display: none;")

        def _parse_required_int_setting(*, raw_value: str, field_label: str) -> int:
            value = raw_value.strip()
            if not value:
                raise ValueError(f"{field_label} must not be empty.")
            try:
                return int(value)
            except ValueError as xcp:
                raise ValueError(f"{field_label} must be a whole number.") from xcp

        async def _handle_discord_settings_submit(_: object | None = None) -> None:
            try:
                next_refresh_interval_seconds = (
                    activity_settings.refresh_interval_seconds
                    if not can_edit_refresh_rate
                    else _parse_required_int_setting(
                        raw_value=_value_as_text(refresh_interval_input),
                        field_label="Tick duration",
                    )
                )
                next_settings = config.DiscordSettings(
                    activity=config.DiscordActivitySettings(
                        fallback_text=_value_as_text(fallback_text_input).strip(),
                        prefix=_value_as_text(prefix_input),
                        separator=_value_as_text(separator_input),
                        suffix=_value_as_text(suffix_input),
                        refresh_interval_seconds=next_refresh_interval_seconds,
                        units_per_app=_parse_required_int_setting(
                            raw_value=_value_as_text(units_per_app_input),
                            field_label="Ticks per app",
                        ),
                        alt_text_percentage=_parse_required_int_setting(
                            raw_value=_value_as_text(alt_text_percentage_input),
                            field_label="Alt-text percentage",
                        ),
                        fields=config.parse_discord_activity_fields(
                            _value_as_text(field_order_input),
                            source="Discord activity field order",
                        ),
                    )
                )
            except (TypeError, ValueError) as xcp:
                ui.notify(str(xcp), type="negative")
                return

            try:
                result = await self._node_api.mutate_discord_settings(
                    settings=next_settings,
                    actor_user_id=user.discord_id,
                )
            except Exception as xcp:
                log.warning("Discord settings update failed: user=%s error=%s", user.discord_id, xcp)
                ui.notify(f"Discord settings update failed: {xcp}", type="negative")
                return

            ui.notify(result.message, type="positive")
            _hide_discord_settings_panel()
            ui.navigate.reload()

        with ui.element("div").classes("mod-node-settings-overlay").style("display: none;") as discord_settings_overlay:
            backdrop = ui.element("div").classes("mod-node-settings-backdrop")
            backdrop.on("click", lambda _: _hide_discord_settings_panel())
            panel_shell = ui.element("div").classes("mod-node-settings-shell")
            panel_shell.on("click", js_handler="(event) => event.stopPropagation()")
            with panel_shell:
                with ui.card().classes("mod-card mod-dialog-card mod-app-details-dialog-card"):
                    with ui.column().classes("w-full gap-4 mod-app-details-layout"):
                        with ui.column().classes("gap-1"):
                            ui.label("Discord Settings").classes("text-xl font-black mod-title-small")
                            ui.label("Configure how this bot presents/acts on Discord.").classes("mod-subtitle text-sm")
                        with ui.column().classes("mod-app-details-section"):
                            ui.label("Activity Status").classes("mod-stat-label")
                            ui.label(
                                f"Available segments: {available_fields_text}. Use a comma-separated order.",
                            ).classes("mod-subtitle text-xs")
                            fallback_text_input = (
                                ui.input("Fallback text", value=activity_settings.fallback_text)
                                .props("filled square dense clearable hide-bottom-space color=accent maxlength=80")
                                .classes("mod-app-details-field")
                            )
                            with ui.row().classes("w-full gap-2 flex-wrap"):
                                refresh_interval_input = (
                                    ui.input(
                                        "Tick duration",
                                        value=str(activity_settings.refresh_interval_seconds),
                                    )
                                    .props(
                                        "filled square dense hide-bottom-space color=accent "
                                        "type=number inputmode=numeric step=1 min=1 max=60"
                                    )
                                    .classes("mod-app-details-field mod-app-details-point-field")
                                )
                                units_per_app_input = (
                                    ui.input(
                                        "Ticks per app",
                                        value=str(activity_settings.units_per_app),
                                    )
                                    .props(
                                        "filled square dense hide-bottom-space color=accent "
                                        "type=number inputmode=numeric step=1 min=1 max=20"
                                    )
                                    .classes("mod-app-details-field mod-app-details-point-field")
                                )
                                alt_text_percentage_input = (
                                    ui.input(
                                        "Alt-text percentage",
                                        value=str(activity_settings.alt_text_percentage),
                                    )
                                    .props(
                                        "filled square dense hide-bottom-space color=accent "
                                        "type=number inputmode=numeric step=1 min=0 max=100"
                                    )
                                    .classes("mod-app-details-field mod-app-details-point-field")
                                )
                            if not can_edit_refresh_rate:
                                refresh_interval_input.disable()
                            with ui.row().classes("w-full gap-2 flex-wrap"):
                                prefix_input = (
                                    ui.input("Prefix", value=activity_settings.prefix)
                                    .props("filled square dense clearable hide-bottom-space color=accent maxlength=40")
                                    .classes("mod-app-details-field")
                                )
                                separator_input = (
                                    ui.input("Separator", value=activity_settings.separator)
                                    .props("filled square dense hide-bottom-space color=accent maxlength=16")
                                    .classes("mod-app-details-field")
                                )
                            suffix_input = (
                                ui.input("Suffix", value=activity_settings.suffix)
                                .props("filled square dense clearable hide-bottom-space color=accent maxlength=40")
                                .classes("mod-app-details-field")
                            )
                            field_order_input = (
                                ui.input(
                                    "Segment order",
                                    value=config.format_discord_activity_fields(activity_settings.fields),
                                )
                                .props("filled square dense hide-bottom-space color=accent")
                                .classes("mod-app-details-field")
                            )
                        with ui.row().classes("w-full justify-end gap-2 mod-app-details-actions"):
                            ui.button("Cancel", on_click=lambda _: _hide_discord_settings_panel()).classes(
                                "mod-list-button secondary"
                            )
                            ui.button("Save", on_click=_handle_discord_settings_submit).classes("mod-list-button")

        return _show_discord_settings_panel

    def _render_discord_settings_control(self, *, ui: ModWebUi, user: ModWebUser) -> None:
        open_panel = self._build_discord_settings_panel(ui=ui, user=user)
        ui.button(
            "Discord",
            on_click=lambda _: open_panel(),
        ).classes(f"{MOD_WEB_ACTION_BASE_CLASSES} px-4 py-2 text-sm")

    def _build_fake_chat_preview_panel(self, *, ui: ModWebUi) -> Callable[[], None]:
        context_options: dict[str, str] = self._fake_chat_preview_app_options()
        send_target_options: dict[str, str] = self._fake_chat_preview_send_target_options()
        source_options: dict[str, ChatEndpointKind] = {
            "Game": ChatEndpointKind.APP,
            "Discord": ChatEndpointKind.DISCORD_CHANNEL,
            "Web": ChatEndpointKind.WEB_SESSION,
            "System": ChatEndpointKind.SYSTEM,
        }
        author_options: dict[str, ChatAuthorKind] = {
            "Game Player": ChatAuthorKind.GAME_PLAYER,
            "Discord User": ChatAuthorKind.DISCORD_USER,
            "Web User": ChatAuthorKind.WEB_USER,
            "System": ChatAuthorKind.SYSTEM,
        }
        message_options: dict[str, _ModWebFakeChatMessageMode] = {
            "Text": _ModWebFakeChatMessageMode.TEXT,
            "Join": _ModWebFakeChatMessageMode.JOIN,
            "Leave": _ModWebFakeChatMessageMode.LEAVE,
            "Death": _ModWebFakeChatMessageMode.DEATH,
            "PVP Kill": _ModWebFakeChatMessageMode.PVP_KILL,
            "Advancement": _ModWebFakeChatMessageMode.ADVANCEMENT,
            "Goal": _ModWebFakeChatMessageMode.GOAL,
            "Challenge": _ModWebFakeChatMessageMode.CHALLENGE,
            "Research": _ModWebFakeChatMessageMode.RESEARCH,
            "Game Event": _ModWebFakeChatMessageMode.GAME_EVENT,
            "App Started": _ModWebFakeChatMessageMode.APP_STARTED,
            "App Stopped": _ModWebFakeChatMessageMode.APP_STOPPED,
            "App Crashed": _ModWebFakeChatMessageMode.APP_CRASHED,
            "Maintenance Warning": _ModWebFakeChatMessageMode.MAINTENANCE_WARNING,
            "Bot Started": _ModWebFakeChatMessageMode.BOT_STARTED,
            "Bot Error": _ModWebFakeChatMessageMode.BOT_ERROR,
            "Embed": _ModWebFakeChatMessageMode.EMBED,
        }
        reference_options: dict[str, ChatReferenceKind] = {
            "None": ChatReferenceKind.NONE,
            "Reply": ChatReferenceKind.REPLY,
            "Forward": ChatReferenceKind.FORWARD,
        }
        initial_app_label: str | None = next(iter(context_options), None)
        initial_publish_target_label: str | None = next(iter(send_target_options), None)
        initial_app_name: str | None = (
            context_options.get(initial_app_label)
            if initial_app_label is not None
            else (
                send_target_options.get(initial_publish_target_label)
                if initial_publish_target_label is not None
                else None
            )
        )
        state: _ModWebFakeChatPreviewState = _ModWebFakeChatPreviewState(app_name=initial_app_name)
        publish_target_label: str | None = initial_publish_target_label
        initial_source_label: str = next(
            label for label, option in source_options.items() if option is state.source_kind
        )
        initial_author_label: str = next(
            label for label, option in author_options.items() if option is state.author_kind
        )
        initial_message_label: str = next(
            label for label, option in message_options.items() if option is state.message_mode
        )
        initial_reference_label: str = next(
            label for label, option in reference_options.items() if option is state.reference_kind
        )
        mode_help_label: Label | None = None

        @ui.refreshable
        def _preview_body() -> None:
            try:
                preview_event: ChatEvent = self._build_fake_chat_preview_event(state)
            except ValueError as xcp:
                ui.label(str(xcp)).classes("mod-subtitle text-sm mod-error-text")
                return

            def ignore_preview_reply(_event: ChatEvent) -> None:
                return None

            with ui.column().classes("mod-chat-timeline-shell w-full").style("min-height: auto;"):
                with (
                    ui.column()
                    .classes("mod-chat-timeline w-full")
                    .style("min-height: 0; max-height: none; overflow: visible;")
                ):
                    self._render_chat_event_group(
                        ui=ui,
                        group=_ModWebChatEventGroup(head_event=preview_event, events=(preview_event,)),
                        room_id=preview_event.room_id,
                        can_reply=False,
                        on_reply=ignore_preview_reply,
                    )

        def _refresh_preview() -> None:
            _preview_body.refresh()

        def _update_app_name(value: object) -> None:
            if value is None:
                state.app_name = None
            else:
                state.app_name = context_options.get(str(value).strip())
            _refresh_preview()

        def _update_source_kind(value: object) -> None:
            if value is not None:
                option: ChatEndpointKind | None = source_options.get(str(value).strip())
                if option is not None:
                    state.source_kind = option
            _refresh_preview()

        def _update_author_kind(value: object) -> None:
            if value is not None:
                option: ChatAuthorKind | None = author_options.get(str(value).strip())
                if option is not None:
                    state.author_kind = option
            _refresh_preview()

        def _update_message_mode(value: object) -> None:
            if value is not None:
                option: _ModWebFakeChatMessageMode | None = message_options.get(str(value).strip())
                if option is not None:
                    state.message_mode = option
                    if mode_help_label is not None:
                        mode_help_label.set_text(self._fake_chat_preview_mode_help_text(option))
            _refresh_preview()

        def _update_publish_target(value: object) -> None:
            nonlocal publish_target_label
            if value is None:
                publish_target_label = None
            else:
                publish_target_label = str(value).strip() or None

        def _update_reference_kind(value: object) -> None:
            if value is not None:
                option: ChatReferenceKind | None = reference_options.get(str(value).strip())
                if option is not None:
                    state.reference_kind = option
            _refresh_preview()

        def _event_text(value: object) -> str:
            return str(value or "")

        def _handle_app_name_change(event: ModWebValueContainer) -> None:
            _update_app_name(_value_as_object(event))

        def _handle_source_kind_change(event: ModWebValueContainer) -> None:
            _update_source_kind(_value_as_object(event))

        def _handle_author_kind_change(event: ModWebValueContainer) -> None:
            _update_author_kind(_value_as_object(event))

        def _handle_message_mode_change(event: ModWebValueContainer) -> None:
            _update_message_mode(_value_as_object(event))

        def _handle_author_name_change(event: ModWebValueContainer) -> None:
            state.author_name = _event_text(_value_as_object(event))
            _refresh_preview()

        def _handle_source_label_change(event: ModWebValueContainer) -> None:
            state.source_label = _event_text(_value_as_object(event))
            _refresh_preview()

        def _handle_content_text_change(event: ModWebValueContainer) -> None:
            state.content_text = _event_text(_value_as_object(event))
            _refresh_preview()

        def _handle_detail_text_change(event: ModWebValueContainer) -> None:
            state.detail_text = _event_text(_value_as_object(event))
            _refresh_preview()

        def _handle_embed_title_change(event: ModWebValueContainer) -> None:
            state.embed_title = _event_text(_value_as_object(event))
            _refresh_preview()

        def _handle_embed_description_change(event: ModWebValueContainer) -> None:
            state.embed_description = _event_text(_value_as_object(event))
            _refresh_preview()

        def _handle_publish_target_change(event: ModWebValueContainer) -> None:
            _update_publish_target(_value_as_object(event))

        def _handle_reference_kind_change(event: ModWebValueContainer) -> None:
            _update_reference_kind(_value_as_object(event))

        def _handle_reference_author_change(event: ModWebValueContainer) -> None:
            state.reference_author_name = _event_text(_value_as_object(event))
            _refresh_preview()

        def _handle_reference_content_change(event: ModWebValueContainer) -> None:
            state.reference_content = _event_text(_value_as_object(event))
            _refresh_preview()

        def _handle_link_url_change(event: ModWebValueContainer) -> None:
            state.link_url = _event_text(_value_as_object(event))
            _refresh_preview()

        def _handle_link_label_change(event: ModWebValueContainer) -> None:
            state.link_label = _event_text(_value_as_object(event))
            _refresh_preview()

        def _handle_attachment_url_change(event: ModWebValueContainer) -> None:
            state.attachment_url = _event_text(_value_as_object(event))
            _refresh_preview()

        def _handle_attachment_name_change(event: ModWebValueContainer) -> None:
            state.attachment_name = _event_text(_value_as_object(event))
            _refresh_preview()

        def _handle_author_color_change(event: ModWebValueContainer) -> None:
            state.author_color_hex = _event_text(_value_as_object(event))
            _refresh_preview()

        def _handle_author_avatar_change(event: ModWebValueContainer) -> None:
            state.author_avatar_uri = _event_text(_value_as_object(event))
            _refresh_preview()

        async def _publish_preview_event() -> None:
            relay: WebChatRelayPublisher | None = self._chat_relay
            if relay is None:
                ui.notify("Chat relay is not available on this node.", type="negative")
                return
            if publish_target_label is None:
                ui.notify("Select a target chat hub first.", type="warning")
                return
            target_room_id: str | None = send_target_options.get(publish_target_label)
            if target_room_id is None:
                ui.notify("Selected chat hub is invalid.", type="negative")
                return
            try:
                event: ChatEvent = self._build_fake_chat_preview_event_for_room(state, room_id=target_room_id)
                await relay.publish_chat_event(event=event)
            except Exception as xcp:
                log.warning("Fake chat publish failed: room=%s error=%s", target_room_id, xcp)
                ui.notify(f"Fake chat publish failed: {xcp}", type="negative")
                return
            ui.notify(f"Sent fake chat event to {target_room_id}.", type="positive")

        with ui.dialog() as preview_dialog:
            with ui.card().classes("mod-card mod-dialog-card mod-fake-chat-dialog-card"):
                with ui.column().classes("w-full gap-4 p-5"):
                    with ui.column().classes("gap-0"):
                        ui.label("Fake Chat Preview").classes("text-xl font-black mod-title-small")
                        ui.label("Preview a synthetic chat event without publishing it anywhere.").classes(
                            "mod-subtitle text-sm"
                        )
                        mode_help_label = ui.label(self._fake_chat_preview_mode_help_text(state.message_mode)).classes(
                            "mod-subtitle text-xs"
                        )
                    with ui.grid(columns=2).classes("w-full gap-3"):
                        ui.select(
                            list[str](context_options),
                            value=initial_app_label,
                            label="App Context",
                            on_change=_handle_app_name_change,
                        ).props(self._fake_chat_select_props(clearable=True)).classes("w-full mod-fake-chat-field")
                        ui.select(
                            list[str](source_options),
                            value=initial_source_label,
                            label="Source",
                            on_change=_handle_source_kind_change,
                        ).props(self._fake_chat_select_props(clearable=False)).classes("w-full mod-fake-chat-field")
                        ui.select(
                            list[str](author_options),
                            value=initial_author_label,
                            label="Author Type",
                            on_change=_handle_author_kind_change,
                        ).props(self._fake_chat_select_props(clearable=False)).classes("w-full mod-fake-chat-field")
                        ui.select(
                            list[str](message_options),
                            value=initial_message_label,
                            label="Message Type",
                            on_change=_handle_message_mode_change,
                        ).props(self._fake_chat_select_props(clearable=False)).classes("w-full mod-fake-chat-field")
                        ui.select(
                            list[str](reference_options),
                            value=initial_reference_label,
                            label="Reference",
                            on_change=_handle_reference_kind_change,
                        ).props(self._fake_chat_select_props(clearable=False)).classes("w-full mod-fake-chat-field")
                        (
                            ui.input(
                                label="Author Name",
                                value=state.author_name,
                                on_change=_handle_author_name_change,
                            )
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full mod-fake-chat-field")
                        )
                        (
                            ui.input(
                                label="Author Color",
                                value=state.author_color_hex,
                                on_change=_handle_author_color_change,
                            )
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full mod-fake-chat-field")
                        )
                        (
                            ui.input(
                                label="Author Avatar URL",
                                value=state.author_avatar_uri,
                                on_change=_handle_author_avatar_change,
                            )
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full col-span-2 mod-fake-chat-field")
                        )
                        (
                            ui.input(
                                label="Source Label",
                                value=state.source_label,
                                on_change=_handle_source_label_change,
                            )
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full mod-fake-chat-field")
                        )
                        (
                            ui.input(
                                label="Content",
                                value=state.content_text,
                                on_change=_handle_content_text_change,
                            )
                            .props("filled square type=textarea autogrow hide-bottom-space color=accent")
                            .classes("w-full col-span-2 mod-fake-chat-field")
                        )
                        (
                            ui.input(
                                label="Detail / Cause / Summary",
                                value=state.detail_text,
                                on_change=_handle_detail_text_change,
                            )
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full mod-fake-chat-field")
                        )
                        (
                            ui.input(
                                label="Primary Label / Title",
                                value=state.embed_title,
                                on_change=_handle_embed_title_change,
                            )
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full mod-fake-chat-field")
                        )
                        (
                            ui.input(
                                label="Secondary Text / Description",
                                value=state.embed_description,
                                on_change=_handle_embed_description_change,
                            )
                            .props("filled square type=textarea autogrow hide-bottom-space color=accent")
                            .classes("w-full col-span-2 mod-fake-chat-field")
                        )
                        (
                            ui.input(
                                label="Reference Author",
                                value=state.reference_author_name,
                                on_change=_handle_reference_author_change,
                            )
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full mod-fake-chat-field")
                        )
                        (
                            ui.input(
                                label="Reference Content",
                                value=state.reference_content,
                                on_change=_handle_reference_content_change,
                            )
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full mod-fake-chat-field")
                        )
                        (
                            ui.input(
                                label="Link URL",
                                value=state.link_url,
                                on_change=_handle_link_url_change,
                            )
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full mod-fake-chat-field")
                        )
                        (
                            ui.input(
                                label="Link Label",
                                value=state.link_label,
                                on_change=_handle_link_label_change,
                            )
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full mod-fake-chat-field")
                        )
                        (
                            ui.input(
                                label="Attachment URL",
                                value=state.attachment_url,
                                on_change=_handle_attachment_url_change,
                            )
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full mod-fake-chat-field")
                        )
                        (
                            ui.input(
                                label="Attachment Name",
                                value=state.attachment_name,
                                on_change=_handle_attachment_name_change,
                            )
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full mod-fake-chat-field")
                        )
                    ui.label("Preview").classes("mod-section-label")
                    _preview_body()
                    with ui.row().classes("mod-fake-chat-footer w-full"):
                        with ui.row().classes("items-end gap-2 flex-wrap"):
                            ui.select(
                                list[str](send_target_options),
                                value=publish_target_label,
                                label="Send To Hub",
                                on_change=_handle_publish_target_change,
                            ).props(self._fake_chat_select_props(clearable=True)).classes(
                                "mod-fake-chat-field mod-fake-chat-send-target"
                            )
                            send_button: Button = ui.button("Send for Real", on_click=_publish_preview_event).classes(
                                "mod-list-button"
                            )
                            if self._chat_relay is None or not send_target_options:
                                send_button.disable()
                        ui.button("Close", on_click=preview_dialog.close).classes("mod-list-button secondary")
        return preview_dialog.open

    def _render_fake_chat_preview_control(self, *, ui: ModWebUi) -> None:
        open_panel = self._build_fake_chat_preview_panel(ui=ui)
        ui.button("Fake Chat", on_click=open_panel).classes(f"{MOD_WEB_ACTION_BASE_CLASSES} px-4 py-2 text-sm")

    def _fake_chat_preview_app_options(self) -> dict[str, str]:
        managed_room_ids = [app.name for app in self._managed_apps()]
        managed_room_id_set = set(managed_room_ids)
        room_ids = managed_room_ids + [
            room_id for room_id in ChatHub().bound_room_ids() if room_id not in managed_room_id_set
        ]
        return self._fake_chat_preview_room_options(room_ids)

    def _fake_chat_preview_send_target_options(self) -> dict[str, str]:
        bound_room_ids = ChatHub().bound_room_ids()
        if bound_room_ids:
            return self._fake_chat_preview_room_options(bound_room_ids)

        relay_room_ids = [app.name for app in self._managed_apps() if app.supports_chat_relay]
        if relay_room_ids:
            return self._fake_chat_preview_room_options(relay_room_ids)

        return self._fake_chat_preview_room_options(app.name for app in self._managed_apps())

    def _fake_chat_preview_room_options(self, room_ids: Iterable[str]) -> dict[str, str]:
        options: list[tuple[str, str]] = []
        seen_room_ids: set[str] = set()
        for raw_room_id in room_ids:
            room_id = raw_room_id.strip()
            if not room_id or room_id in seen_room_ids:
                continue
            seen_room_ids.add(room_id)
            options.append((self._fake_chat_preview_room_label(room_id), room_id))
        options.sort(key=lambda item: (item[0].casefold(), item[1].casefold()))
        return {label: room_id for label, room_id in options}

    def _fake_chat_preview_room_label(self, room_id: str) -> str:
        app = self._chat_room_app(room_id)
        if app is None:
            return room_id
        friendly = getattr(app, "friendly", None)
        if isinstance(friendly, str) and friendly.strip():
            return f"{friendly.strip()} ({room_id})"
        return room_id

    def _build_fake_chat_preview_event(self, state: _ModWebFakeChatPreviewState) -> ChatEvent:
        return self._build_fake_chat_preview_event_for_room(state, room_id=state.app_name or "preview_room")

    def _build_fake_chat_preview_event_for_room(self, state: _ModWebFakeChatPreviewState, *, room_id: str) -> ChatEvent:
        source: ChatEndpointId = self._fake_chat_preview_source_id(source_kind=state.source_kind, room_id=room_id)
        author: ChatAuthor = self._fake_chat_preview_author(state)
        notice_source = self._fake_chat_preview_notice_source(state.source_kind)
        app_name: str = self._fake_chat_preview_app_name(room_id=room_id)
        notice: RelayNotice | None = self._fake_chat_preview_notice(state=state, notice_source=notice_source)
        if notice is not None:
            return self._fake_chat_preview_chat_event(
                room_id=room_id,
                source=source,
                author=author,
                state=state,
                content=render_notice_text(notice, author_name=author.display_name, app_name=app_name),
                notice=notice,
            )
        if state.message_mode is _ModWebFakeChatMessageMode.EMBED:
            embed_title: str = state.embed_title.strip() or "Preview"
            embed_description: str = state.embed_description.strip() or "Preview details"
            embed_color: int = self._fake_chat_preview_embed_color(room_id=room_id)
            return self._fake_chat_preview_chat_event(
                room_id=room_id,
                source=source,
                author=author,
                state=state,
                content=state.content_text.strip() or f"{embed_title}: {embed_description}",
                embed=ChatEmbed(title=embed_title, description=embed_description, color=embed_color),
            )
        content = state.content_text.strip() or "hello from preview"
        return self._fake_chat_preview_chat_event(
            room_id=room_id,
            source=source,
            author=author,
            state=state,
            content=content,
        )

    @staticmethod
    def _fake_chat_preview_mode_help_text(mode: _ModWebFakeChatMessageMode) -> str:
        descriptions: dict[_ModWebFakeChatMessageMode, str] = {
            _ModWebFakeChatMessageMode.TEXT: "Freeform chat message with optional reply, link, and attachment preview.",
            _ModWebFakeChatMessageMode.JOIN: "Player session notice. Body hides in chat and becomes a Joined badge.",
            _ModWebFakeChatMessageMode.LEAVE: "Player session notice. Body hides in chat and becomes a Left badge.",
            _ModWebFakeChatMessageMode.DEATH: "Player death notice with a cause string.",
            _ModWebFakeChatMessageMode.PVP_KILL: "PVP death notice with a killer name or detail string.",
            _ModWebFakeChatMessageMode.ADVANCEMENT: "Game progress notice with badge and embed rendering.",
            _ModWebFakeChatMessageMode.GOAL: "Goal progress notice with badge and embed rendering.",
            _ModWebFakeChatMessageMode.CHALLENGE: "Challenge progress notice with badge and embed rendering.",
            _ModWebFakeChatMessageMode.RESEARCH: "Research progress notice with badge and embed rendering.",
            _ModWebFakeChatMessageMode.GAME_EVENT: "Generic game event notice with a custom label and detail.",
            _ModWebFakeChatMessageMode.APP_STARTED: "App lifecycle started notice using detail text as join address.",
            _ModWebFakeChatMessageMode.APP_STOPPED: "App lifecycle stopped notice using secondary text as detail lines.",
            _ModWebFakeChatMessageMode.APP_CRASHED: "App lifecycle crash notice using detail text as summary.",
            _ModWebFakeChatMessageMode.MAINTENANCE_WARNING: "Maintenance warning notice using detail text as lead minutes.",
            _ModWebFakeChatMessageMode.BOT_STARTED: "Bot startup notice using detail text as auto-launch app name.",
            _ModWebFakeChatMessageMode.BOT_ERROR: "Bot error notice using detail text as the summary.",
            _ModWebFakeChatMessageMode.EMBED: "Custom embed message with optional body text, reply, link, and attachment.",
        }
        return descriptions[mode]

    @staticmethod
    def _fake_chat_preview_detail_lines(value: str) -> tuple[str, ...]:
        return tuple(line.strip() for line in value.splitlines() if line.strip())

    @staticmethod
    def _fake_chat_preview_positive_int(value: str) -> int | None:
        stripped_value = value.strip()
        if not stripped_value:
            return None
        try:
            parsed = int(stripped_value)
        except ValueError:
            return None
        if parsed <= 0:
            return None
        return parsed

    def _fake_chat_preview_notice(
        self,
        *,
        state: _ModWebFakeChatPreviewState,
        notice_source: RelayNoticeSource,
    ) -> RelayNotice | None:
        if state.message_mode is _ModWebFakeChatMessageMode.JOIN:
            return PlayerSessionNotice(action=PlayerSessionAction.JOINED, source=notice_source)
        if state.message_mode is _ModWebFakeChatMessageMode.LEAVE:
            return PlayerSessionNotice(action=PlayerSessionAction.LEFT, source=notice_source)
        if state.message_mode is _ModWebFakeChatMessageMode.DEATH:
            cause = state.detail_text.strip() or "Skeleton"
            return GameDeathNotice(
                death_kind=GameDeathKind.PVE,
                detail_text=f"died to {cause}",
                source=notice_source,
            )
        if state.message_mode is _ModWebFakeChatMessageMode.PVP_KILL:
            cause = state.detail_text.strip() or "Yoko"
            return GameDeathNotice(
                death_kind=GameDeathKind.PVP,
                detail_text=f"killed by {cause}",
                source=notice_source,
            )
        if state.message_mode in {
            _ModWebFakeChatMessageMode.ADVANCEMENT,
            _ModWebFakeChatMessageMode.GOAL,
            _ModWebFakeChatMessageMode.CHALLENGE,
            _ModWebFakeChatMessageMode.RESEARCH,
        }:
            progress_kind, default_label, default_title = self._fake_chat_preview_progress_defaults(state.message_mode)
            return GameProgressNotice(
                progress_kind=progress_kind,
                label=state.embed_title.strip() or default_label,
                title=state.embed_description.strip() or default_title,
                source=notice_source,
            )
        if state.message_mode is _ModWebFakeChatMessageMode.GAME_EVENT:
            return GameEventNotice(
                label=state.embed_title.strip() or "Server Event",
                detail=state.detail_text.strip() or None,
                source=notice_source,
            )
        if state.message_mode is _ModWebFakeChatMessageMode.APP_STARTED:
            return AppLifecycleNotice(
                state=AppLifecycleState.STARTED,
                source=notice_source,
                join_address=state.detail_text.strip() or None,
                detail_lines=self._fake_chat_preview_detail_lines(state.embed_description),
            )
        if state.message_mode is _ModWebFakeChatMessageMode.APP_STOPPED:
            return AppLifecycleNotice(
                state=AppLifecycleState.STOPPED,
                source=notice_source,
                detail_lines=self._fake_chat_preview_detail_lines(state.embed_description),
                summary=state.detail_text.strip() or None,
            )
        if state.message_mode is _ModWebFakeChatMessageMode.APP_CRASHED:
            return AppLifecycleNotice(
                state=AppLifecycleState.CRASHED,
                source=notice_source,
                summary=state.detail_text.strip() or "Unexpected exit",
                detail_lines=self._fake_chat_preview_detail_lines(state.embed_description),
            )
        if state.message_mode is _ModWebFakeChatMessageMode.MAINTENANCE_WARNING:
            return MaintenanceNotice(
                stage=MaintenanceStage.WARNING,
                target=RestartTarget.SYSTEM,
                source=notice_source,
                lead_minutes=self._fake_chat_preview_positive_int(state.detail_text) or 15,
                summary_lines=self._fake_chat_preview_detail_lines(state.embed_description),
            )
        if state.message_mode is _ModWebFakeChatMessageMode.BOT_STARTED:
            return BotLifecycleNotice(
                stage=BotLifecycleStage.STARTED,
                source=notice_source,
                auto_launch_app_names=((state.detail_text.strip(),) if state.detail_text.strip() else ()),
                startup_disabled_lines=self._fake_chat_preview_detail_lines(state.embed_description),
            )
        if state.message_mode is _ModWebFakeChatMessageMode.BOT_ERROR:
            return BotLifecycleNotice(
                stage=BotLifecycleStage.ERROR,
                source=notice_source,
                summary=state.detail_text.strip() or "Preview error",
                error_lines=self._fake_chat_preview_detail_lines(state.embed_description),
            )
        return None

    @staticmethod
    def _fake_chat_preview_progress_defaults(
        mode: _ModWebFakeChatMessageMode,
    ) -> tuple[GameProgressKind, str, str]:
        if mode is _ModWebFakeChatMessageMode.ADVANCEMENT:
            return GameProgressKind.ADVANCEMENT, "Advancement", "Stone Age"
        if mode is _ModWebFakeChatMessageMode.GOAL:
            return GameProgressKind.GOAL, "Goal", "Acquire Hardware"
        if mode is _ModWebFakeChatMessageMode.CHALLENGE:
            return GameProgressKind.CHALLENGE, "Challenge", "How Did We Get Here?"
        if mode is _ModWebFakeChatMessageMode.RESEARCH:
            return GameProgressKind.RESEARCH, "Research", "Automation"
        raise ValueError(f"Unsupported fake chat progress mode: {mode.value}")

    @staticmethod
    def _fake_chat_preview_author(state: _ModWebFakeChatPreviewState) -> ChatAuthor:
        author_name: str | LiteralString = (
            state.author_name.strip() or state.author_kind.value.replace("_", " ").title()
        )
        color_hex: str | None = state.author_color_hex.strip() or None
        avatar_uri: str | None = state.author_avatar_uri.strip() or None
        return ChatAuthor(
            kind=state.author_kind,
            display_name=author_name,
            color_hex=color_hex,
            avatar_uri=avatar_uri,
        )

    @staticmethod
    def _fake_chat_preview_reference(
        state: _ModWebFakeChatPreviewState,
    ) -> tuple[ChatReferenceKind, ChatMessageReference | None]:
        if state.reference_kind is ChatReferenceKind.NONE:
            return ChatReferenceKind.NONE, None
        author_display_name: str = state.reference_author_name.strip() or "Taylor"
        content: str = state.reference_content.strip() or "Previous message"
        return state.reference_kind, ChatMessageReference(author_display_name=author_display_name, content=content)

    @staticmethod
    def _fake_chat_preview_links(state: _ModWebFakeChatPreviewState) -> tuple[ChatLink, ...]:
        url: str = state.link_url.strip()
        if not url:
            return ()
        label: str | None = state.link_label.strip() or None
        return (
            ChatLink(
                url=url,
                label=label,
                is_media=True,
                extension=Path(url).suffix or None,
                provider=ChatMediaProvider.DIRECT,
            ),
        )

    @staticmethod
    def _fake_chat_preview_attachments(state: _ModWebFakeChatPreviewState) -> tuple[ChatAttachment, ...]:
        url: str = state.attachment_url.strip()
        if not url:
            return ()
        name = state.attachment_name.strip() or "preview.bin"
        return (ChatAttachment(uri=url, name=name),)

    def _fake_chat_preview_chat_event(
        self,
        *,
        room_id: str,
        source: ChatEndpointId,
        author: ChatAuthor,
        state: _ModWebFakeChatPreviewState,
        content: str,
        notice: RelayNotice | None = None,
        embed: ChatEmbed | None = None,
    ) -> ChatEvent:
        reference_kind, reference = self._fake_chat_preview_reference(state)
        return ChatEvent(
            room_id=room_id,
            source=source,
            author=author,
            content=content,
            attachments=self._fake_chat_preview_attachments(state),
            links=self._fake_chat_preview_links(state),
            reference_kind=reference_kind,
            reference=reference,
            notice=notice,
            embed=embed,
            source_label=state.source_label.strip() or None,
        )

    def _fake_chat_preview_app_name(self, *, room_id: str) -> str:
        app: object | None = self._chat_room_app(room_id)
        if app is None:
            return room_id
        friendly: Any | None = getattr(app, "friendly", None)
        if isinstance(friendly, str) and friendly.strip():
            return friendly
        return room_id

    @staticmethod
    def _fake_chat_preview_source_id(*, source_kind: ChatEndpointKind, room_id: str) -> ChatEndpointId:
        if source_kind is ChatEndpointKind.APP:
            return ChatEndpointId.app(room_id)
        if source_kind is ChatEndpointKind.DISCORD_CHANNEL:
            return ChatEndpointId.discord_channel("preview")
        if source_kind is ChatEndpointKind.WEB_SESSION:
            return ChatEndpointId.web_session("preview")
        return ChatEndpointId(kind=source_kind, value="preview")

    def _fake_chat_preview_embed_color(self, *, room_id: str) -> int:
        app: object | None = self._chat_room_app(room_id)
        if app is None:
            return 0x8B5CF6
        return int(getattr(app, "manage_embed_color", 0x8B5CF6))

    def _render_app_node_badge(self, *, ui: ModWebUi, node_name: str) -> None:
        with ui.element("div").classes("mod-app-node-badge-wrap"):
            badge: Label = self._badge(ui=ui, text=node_name, tone="black", extra_classes="mod-app-node-badge")
            if color_hex := self._node_role_color_hex(node_name=node_name):
                badge.style(self._node_badge_style(color_hex))


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
