"""StatusPages UI helpers."""

from __future__ import annotations

# ruff: noqa: F403, F405
from .status_support import *


class ModWebStatusPagesMixin(ModWebStatusFeatureSupport):
    @staticmethod
    def _sync_name_cache_with_authority_if_remote(*, name_cache: config.Name_Cache) -> None:
        if config.DATA_AUTHORITY_MODE is not config.DataAuthorityMode.REMOTE:
            return
        sent = name_cache.flush_pending_mutations()
        if sent <= 0:
            raise RuntimeError("No alias authority mutations were delivered.")
        if config.authority_pending_names_path().exists():
            raise RuntimeError("Alias authority sync is incomplete; pending name mutations remain queued.")
        if not name_cache.refresh_from_authority():
            raise RuntimeError("Alias authority refresh failed after syncing mutations.")

    @classmethod
    async def _sync_name_cache_with_authority_if_remote_async(cls, *, name_cache: config.Name_Cache) -> None:
        await run_blocking(cls._sync_name_cache_with_authority_if_remote, name_cache=name_cache)

    @classmethod
    def _persist_alias_draft(
        cls,
        *,
        name_cache: config.Name_Cache,
        target_user_id: int,
        draft: _AliasDraft,
        scopes: tuple[str, ...],
        sync_authority: bool = True,
    ) -> tuple[str, ...]:
        changed_fields: list[str] = []
        if name_cache.set_display_override(target_user_id, draft.display_name):
            changed_fields.append("display name")
        for scope in scopes:
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
        if changed_fields and sync_authority:
            cls._sync_name_cache_with_authority_if_remote(name_cache=name_cache)
        return tuple(changed_fields)

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
        return ModWebStatusPagesMixin._exception_detail_text(exception)

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
            if isinstance(cause, RemoteNodeCircuitOpenError):
                return "This node is recovering from a recent connection failure. Retrying shortly."
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
                    RemoteNodeCircuitOpenError,
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
                                            presence_health_url=(
                                                None if status.is_simulated_down else status.node.presence_health_url
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
        self._apply_theme_for_user(ui=ui, user=user)
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
