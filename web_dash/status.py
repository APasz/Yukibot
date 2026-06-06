from __future__ import annotations

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

from .constants import (
    _APP_RUNTIME_REFRESH_INTERVAL_SECONDS,
    log,
)
from .nicegui_protocols import (
    AsyncRefresh,
    ModWebUi,
    ModWebValueContainer,
    WebChatRelayPublisher,
    _value_as_object,
)
from .runtime_imports import (
    MOD_WEB_ACTION_BASE_CLASSES,
    Awaitable,
    Button,
    ChatAttachment,
    ChatAuthor,
    ChatAuthorKind,
    ChatEmbed,
    ChatEndpointId,
    ChatEndpointKind,
    ChatEvent,
    ChatLink,
    ChatMediaProvider,
    ChatMessageReference,
    ChatReferenceKind,
    Label,
    LiteralString,
    ModWebUser,
    Path,
    Power_Level,
    Request,
    Timer,
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
    _ModWebChatEventGroup,
    _ModWebFakeChatMessageMode,
    _ModWebFakeChatPreviewState,
    _ModWebLinkSpec,
    _ModWebStatusPageConfig,
)

_STATUS_SVG_DIRECTORY: Path = Path(__file__).resolve().parent.parent / "resources" / "svg" / "web_dash"

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
    ) -> None:
        self._apply_theme(ui=ui)
        with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
            self._render_status_page_panel(
                ui=ui,
                config=_ModWebStatusPageConfig(
                    title="Node unavailable",
                    support_text=self._friendly_remote_node_error_text(exception),
                    badge_text="Unavailable",
                    badge_tone="red",
                    accent_color_hex="#dc2626",
                    icon_markup=self._remote_node_unavailable_icon_markup(),
                    context_label=node_name,
                    actions=(_ModWebLinkSpec(label="Home", url=self.index_path()),),
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
                                    self._action_link(ui=ui, label=action.label, url=action.url)
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
    ) -> object:
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
        return ModWebStatusMixin._exception_detail_text(exception)

    @staticmethod
    def _should_render_framework_error_page(*, method: str, path: str, accept_header: str | None) -> bool:
        if method != "GET":
            return False
        if path.startswith(("/api/", "/_nicegui", "/static")):
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
            if isinstance(cause, requests.Timeout):
                return "This node is taking too long to respond. It may be offline or still waking up."
            if isinstance(cause, requests.ConnectionError):
                return "This node is unreachable right now. It may be offline or still waking up."

        detail: str = str(exception).strip()
        if detail.startswith("Remote node rejected the request:"):
            return "This node answered, but it rejected Yuki's request."
        if detail == "Remote node returned invalid JSON." or detail.startswith("Remote node response must be"):
            return "This node answered, but it sent back something Yuki could not understand."
        return "Yuki could not talk to this node right now. Refresh to try again in a moment."

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

    def _authorised_page_user(
        self, *, ui: ModWebUi, request: Request, required_level: Power_Level
    ) -> ModWebUser | None:
        if not self._auth.enabled:
            self._render_auth_setup_page(ui=ui)
            return None
        user: ModWebUser | None = self._auth.current_user(request)
        if user is None:
            self._render_login_page(
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

    def _render_login_page(self, *, ui: ModWebUi, next_path: str, request: Request, show_api_actions: bool) -> None:
        self._apply_theme(ui=ui)
        request_path: str = self._request_path(request)
        simulated_down_node_names: tuple[str, ...] = self._simulated_down_node_names(request)
        node_statuses: tuple[ModWebNodeStatus, ...] = self._login_node_statuses(
            simulated_down_node_names=simulated_down_node_names
        )
        dev_mode_enabled = config.INDEV
        with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
            with ui.card().classes(self._hero_card_classes()):
                with ui.column().classes(self._hero_shell_classes()):
                    with ui.row().classes(self._hero_header_classes()):
                        with ui.column().classes(self._hero_header_main_classes()):
                            ui.label("Yukibot Web").classes(self._hero_title_classes())
                            ui.label(self._login_page_subtitle()).classes(self._hero_support_classes())
                        with ui.column().classes(self._hero_badges_classes()):

                            @ui.refreshable
                            def _render_login_node_badges(current_statuses: tuple[ModWebNodeStatus, ...]) -> None:
                                with ui.row().classes(self._hero_badge_row_classes()):
                                    for status in current_statuses:
                                        badge_toggle_url: str | None = None
                                        badge_tooltip: str | None = None
                                        if dev_mode_enabled and not status.node.is_current:
                                            badge_toggle_url = self._toggle_simulated_down_node_url(
                                                current_url=request_path,
                                                node_name=status.node.node_name,
                                                simulated_down_node_names=simulated_down_node_names,
                                            )
                                            badge_tooltip = (
                                                "Restore this simulated outage."
                                                if status.is_simulated_down
                                                else "Simulate this node going down."
                                            )
                                        self._interactive_badge(
                                            ui=ui,
                                            text=self._login_node_status_badge_text(status),
                                            tone=self._login_node_status_badge_tone(status),
                                            url=badge_toggle_url,
                                            tooltip_text=badge_tooltip,
                                            extra_classes="mod-node-status-badge",
                                        )

                            _render_login_node_badges(node_statuses)
                            refresh_login_node_badges: AsyncRefresh = (
                                self._build_async_refreshable_updater(
                                    refresh_async_value=lambda: asyncio.to_thread(
                                        self._login_node_statuses,
                                        simulated_down_node_names=simulated_down_node_names,
                                    ),
                                    apply_value=_render_login_node_badges.refresh,
                                    error_context="Mod web login node badges",
                                )
                            )
                            refresh_login_node_badges_timer: Timer = ui.timer(
                                _APP_RUNTIME_REFRESH_INTERVAL_SECONDS,
                                lambda: asyncio.create_task(refresh_login_node_badges()),
                            )
                            self._register_timer_cleanup(ui=ui, timer=refresh_login_node_badges_timer)
                    login_show_api_actions: bool = show_api_actions

                    def _set_login_show_api_actions(enabled: bool) -> None:
                        nonlocal login_show_api_actions
                        login_show_api_actions = enabled
                        _render_login_actions.refresh()

                    def _handle_login_show_api_actions_change(event: ModWebValueContainer) -> None:
                        _set_login_show_api_actions(bool(_value_as_object(event)))

                    if self._auth.bypass_enabled:
                        ui.switch(
                            "Show API pill on app lists",
                            value=login_show_api_actions,
                            on_change=_handle_login_show_api_actions_change,
                        ).props("color=accent")

                    @ui.refreshable
                    def _render_login_actions() -> None:
                        with ui.row().classes(self._hero_action_row_classes()):
                            for action in self._login_actions(
                                next_path=next_path,
                                show_api_actions=login_show_api_actions,
                            ):
                                self._action_link(ui=ui, label=action.label, url=action.url)

                    _render_login_actions()
            if dev_mode_enabled:
                self._render_login_dev_error_preview_card(ui=ui)

    def _render_login_dev_error_preview_card(self, *, ui: ModWebUi) -> None:
        with ui.card().classes("mod-card w-full"):
            with ui.column().classes("gap-4 p-5"):
                with ui.column().classes("gap-1"):
                    ui.label("Dev Error Previews").classes("text-xl font-bold mod-title-small")
                    ui.label(
                        "Open the current mod web and NiceGUI error states without breaking a live app page."
                    ).classes("text-sm mod-subtitle")
                with ui.row().classes("gap-2 flex-wrap"):
                    for action in self._dev_error_preview_actions():
                        self._action_link(ui=ui, label=action.label, url=action.url, compact=True)

    def _login_page_subtitle(self) -> str:
        if self._auth.bypass_enabled:
            return "Choose a dev sign-in level to test mod web permissions."
        return "Sign in with Discord to use mod web actions."

    def _login_actions(self, *, next_path: str, show_api_actions: bool) -> tuple[_ModWebLinkSpec, ...]:
        login_next_path: str = self._app_list_view_url(next_path, show_api_actions=show_api_actions)
        if self._auth.bypass_enabled:
            return tuple[_ModWebLinkSpec, ...](
                _ModWebLinkSpec(
                    label=f"Sign in {level.name.title()}",
                    url=f"/auth/dev-login?{urlencode({'level': level.name, 'next_path': login_next_path})}",
                )
                for level in self._auth.bypass_levels
            )
        return (
            _ModWebLinkSpec(
                label="Sign in with Discord",
                url=f"/auth/login?{urlencode({'next_path': login_next_path})}",
            ),
        )

    @staticmethod
    def _dev_error_preview_actions() -> tuple[_ModWebLinkSpec, ...]:
        return (
            _ModWebLinkSpec(label="Access Denied", url="/mod-web/dev/error/access-denied"),
            _ModWebLinkSpec(label="Sign-in Unavailable", url="/mod-web/dev/error/sign-in-unavailable"),
            _ModWebLinkSpec(label="Page Unavailable", url="/mod-web/dev/error/page-unavailable"),
            _ModWebLinkSpec(label="Chat Unavailable", url="/mod-web/dev/error/chat-unavailable"),
            _ModWebLinkSpec(label="Node Unavailable", url="/mod-web/dev/error/node-unavailable"),
            _ModWebLinkSpec(label="Remote JSON Invalid", url="/mod-web/dev/error/remote-json-invalid"),
            _ModWebLinkSpec(label="Remote Timeout", url="/mod-web/dev/error/remote-timeout"),
            _ModWebLinkSpec(label="Remote Rejected", url="/mod-web/dev/error/remote-rejected"),
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
        with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
            with ui.row().classes("items-center gap-3 flex-wrap min-w-0"):
                ui.html(self._user_avatar_markup(avatar_uri=avatar_uri, display_name=display_name))
                ui.label(f"Signed in as {display_name}").classes("text-sm mod-subtitle break-all")
                self._badge(ui=ui, text=self._user_level_label(user), tone=self._user_level_tone(user))
            with ui.row().classes("items-center gap-2 flex-wrap"):
                self._action_link(ui=ui, label="Home", url=self.index_path(), compact=True)
                if self._user_can_use_fake_chat_preview(user):
                    self._render_fake_chat_preview_control(ui=ui)
                self._action_link(ui=ui, label="Log out", url="/auth/logout", compact=True)

    def _user_can_use_fake_chat_preview(self, user: ModWebUser) -> bool:
        return self._user_has_level(user, Power_Level.root)

    def _render_fake_chat_preview_control(self, *, ui: ModWebUi) -> None:
        app_options: dict[str, str] = self._fake_chat_preview_app_options()
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
        initial_app_label: str | None = next(iter(app_options), None)
        initial_app_name: str | None = app_options.get(initial_app_label) if initial_app_label is not None else None
        state: _ModWebFakeChatPreviewState = _ModWebFakeChatPreviewState(app_name=initial_app_name)
        publish_target_label: str | None = initial_app_label
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
                        can_reply=False,
                        on_reply=ignore_preview_reply,
                    )

        def _refresh_preview() -> None:
            _preview_body.refresh()

        def _update_app_name(value: object) -> None:
            if value is None:
                state.app_name = None
            else:
                state.app_name = app_options.get(str(value).strip())
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
            target_room_id: str | None = app_options.get(publish_target_label)
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
                            list[str](app_options),
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
                                list[str](app_options),
                                value=publish_target_label,
                                label="Send To Hub",
                                on_change=_handle_publish_target_change,
                            ).props(self._fake_chat_select_props(clearable=True)).classes(
                                "mod-fake-chat-field mod-fake-chat-send-target"
                            )
                            send_button: Button = ui.button("Send for Real", on_click=_publish_preview_event).classes(
                                "mod-list-button"
                            )
                            if self._chat_relay is None or not app_options:
                                send_button.disable()
                        ui.button("Close", on_click=preview_dialog.close).classes("mod-list-button secondary")

        ui.button("Fake Chat", on_click=preview_dialog.open).classes(f"{MOD_WEB_ACTION_BASE_CLASSES} px-4 py-2 text-sm")

    def _fake_chat_preview_app_options(self) -> dict[str, str]:
        return {f"{app.friendly} ({app.name})": app.name for app in self._managed_apps() if app.supports_chat_relay}

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
                auto_launch_app_name=state.detail_text.strip() or None,
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
        color_hex = state.author_color_hex.strip() or None
        avatar_uri = state.author_avatar_uri.strip() or None
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
        author_display_name = state.reference_author_name.strip() or "Taylor"
        content = state.reference_content.strip() or "Previous message"
        return state.reference_kind, ChatMessageReference(author_display_name=author_display_name, content=content)

    @staticmethod
    def _fake_chat_preview_links(state: _ModWebFakeChatPreviewState) -> tuple[ChatLink, ...]:
        url = state.link_url.strip()
        if not url:
            return ()
        label = state.link_label.strip() or None
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
        url = state.attachment_url.strip()
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
        friendly = getattr(app, "friendly", None)
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
