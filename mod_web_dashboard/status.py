from __future__ import annotations

from .runtime_imports import (
    Awaitable,
    Button,
    ChatAuthor,
    ChatAuthorKind,
    ChatEmbed,
    ChatEndpointId,
    ChatEndpointKind,
    ChatEvent,
    Generics,
    Label,
    LiteralString,
    MOD_WEB_ACTION_BASE_CLASSES,
    ModWebUser,
    Power_Level,
    Request,
    Timer,
    asyncio,
    cast,
    config,
    inspect,
    requests,
    urlencode,
)
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
from .types import (
    ModWebNodeStatus,
    _ModWebChatEventGroup,
    _ModWebFakeChatMessageMode,
    _ModWebFakeChatPreviewState,
    _ModWebLinkSpec,
    _ModWebStatusPageConfig,
)

from .service_base import ModWebServiceSupport
class ModWebStatusMixin(ModWebServiceSupport):
    def _render_error_page(self, *, ui: ModWebUi, title: str, detail: str, app_name: str | None = None) -> None:
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
                if config.icon_markup is not None:
                    ui.html(config.icon_markup).classes("mod-status-figure")
                with ui.row().classes(self._hero_header_classes()):
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
        return """
<svg viewBox="0 0 96 96" aria-hidden="true" class="mod-status-figure-svg">
  <circle cx="48" cy="50" r="24" fill="rgba(24, 24, 27, 0.94)" stroke="rgba(248, 113, 113, 0.6)" stroke-width="3"/>
  <path d="M28 36 35 18l9 14" fill="rgba(24, 24, 27, 0.94)" stroke="rgba(248, 113, 113, 0.6)" stroke-width="3" stroke-linejoin="round"/>
  <path d="M68 36 61 18l-9 14" fill="rgba(24, 24, 27, 0.94)" stroke="rgba(248, 113, 113, 0.6)" stroke-width="3" stroke-linejoin="round"/>
  <circle cx="39" cy="50" r="3.2" fill="rgba(244, 244, 245, 0.92)"/>
  <circle cx="57" cy="50" r="3.2" fill="rgba(244, 244, 245, 0.92)"/>
  <path d="M48 56 44.5 60h7Z" fill="rgba(248, 113, 113, 0.9)"/>
  <path d="M44.5 62c1 2.1 2.2 3.1 3.5 3.1s2.5-1 3.5-3.1" fill="none" stroke="rgba(244, 244, 245, 0.82)" stroke-width="2.4" stroke-linecap="round"/>
  <path d="M28 56h11M27 62h10M57 56h11M59 62h10" fill="none" stroke="rgba(244, 244, 245, 0.55)" stroke-width="2.4" stroke-linecap="round"/>
  <circle cx="72" cy="28" r="10" fill="rgba(127, 29, 29, 0.88)" stroke="rgba(248, 113, 113, 0.72)" stroke-width="2.5"/>
  <path d="M72 23v6m0 6h.01" stroke="rgba(254, 242, 242, 0.94)" stroke-width="2.8" stroke-linecap="round"/>
</svg>
""".strip()

    @staticmethod
    def _framework_error_icon_markup() -> str:
        return """
<svg viewBox="0 0 96 96" aria-hidden="true" class="mod-status-figure-svg">
  <rect x="18" y="20" width="60" height="44" rx="0" fill="rgba(9, 9, 13, 0.88)" stroke="rgba(248, 113, 113, 0.68)" stroke-width="3"/>
  <rect x="26" y="30" width="44" height="8" rx="0" fill="rgba(248, 113, 113, 0.2)" stroke="rgba(248, 113, 113, 0.38)" stroke-width="2"/>
  <rect x="26" y="46" width="28" height="6" rx="0" fill="rgba(244, 244, 245, 0.12)"/>
  <circle cx="65" cy="49" r="5" fill="rgba(248, 113, 113, 0.9)"/>
  <path d="M48 12v8M36 12h24" stroke="rgba(248, 113, 113, 0.72)" stroke-width="3" stroke-linecap="square"/>
  <path d="M34 76h28M58 76h4" stroke="rgba(244, 244, 245, 0.52)" stroke-width="4" stroke-linecap="square"/>
</svg>
""".strip()

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
            "Embed": _ModWebFakeChatMessageMode.EMBED,
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
            _refresh_preview()

        def _update_publish_target(value: object) -> None:
            nonlocal publish_target_label
            if value is None:
                publish_target_label = None
            else:
                publish_target_label = str(value).strip() or None

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
                                label="Detail / Cause",
                                value=state.detail_text,
                                on_change=_handle_detail_text_change,
                            )
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full mod-fake-chat-field")
                        )
                        (
                            ui.input(
                                label="Embed Title",
                                value=state.embed_title,
                                on_change=_handle_embed_title_change,
                            )
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("w-full mod-fake-chat-field")
                        )
                        (
                            ui.input(
                                label="Embed Description",
                                value=state.embed_description,
                                on_change=_handle_embed_description_change,
                            )
                            .props("filled square type=textarea autogrow hide-bottom-space color=accent")
                            .classes("w-full col-span-2 mod-fake-chat-field")
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
        author_name: str | LiteralString = (
            state.author_name.strip() or state.author_kind.value.replace("_", " ").title()
        )
        author: ChatAuthor = ChatAuthor(kind=state.author_kind, display_name=author_name)
        source_label: str | None = state.source_label.strip() or None
        if state.message_mode is _ModWebFakeChatMessageMode.JOIN:
            return ChatEvent(
                room_id=room_id,
                source=source,
                author=author,
                content=Generics.join.value,
                is_template=True,
                source_label=source_label,
            )
        if state.message_mode is _ModWebFakeChatMessageMode.LEAVE:
            return ChatEvent(
                room_id=room_id,
                source=source,
                author=author,
                content=Generics.left.value,
                is_template=True,
                source_label=source_label,
            )
        if state.message_mode is _ModWebFakeChatMessageMode.DEATH:
            return ChatEvent(
                room_id=room_id,
                source=source,
                author=author,
                content=Generics.died_pve.value,
                is_template=True,
                template_values={"cause": state.detail_text.strip() or "Skeleton"},
                source_label=source_label,
            )
        if state.message_mode is _ModWebFakeChatMessageMode.PVP_KILL:
            return ChatEvent(
                room_id=room_id,
                source=source,
                author=author,
                content=Generics.died_pvp.value,
                is_template=True,
                template_values={"cause": state.detail_text.strip() or "Alex"},
                source_label=source_label,
            )
        if state.message_mode is _ModWebFakeChatMessageMode.EMBED:
            embed_title: str = state.embed_title.strip() or "Preview"
            embed_description: str = state.embed_description.strip() or "Preview details"
            embed_color: int = self._fake_chat_preview_embed_color(room_id=room_id)
            return ChatEvent(
                room_id=room_id,
                source=source,
                author=author,
                content=state.content_text.strip() or f"{embed_title}: {embed_description}",
                embed=ChatEmbed(title=embed_title, description=embed_description, color=embed_color),
                source_label=source_label,
            )
        content = state.content_text.strip() or "hello from preview"
        return ChatEvent(
            room_id=room_id,
            source=source,
            author=author,
            content=content,
            source_label=source_label,
        )

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
