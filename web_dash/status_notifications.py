"""StatusNotifications UI helpers."""

from __future__ import annotations

# ruff: noqa: F403, F405
from .status_support import *


class ModWebStatusNotificationsMixin(ModWebStatusFeatureSupport):
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
            f" background: {ModWebStatusNotificationsMixin._notification_tray_badge_background(item)};"
        )

    @staticmethod
    def _notification_tray_node_stripe_style(item: _ModWebNotificationTrayItem) -> str:
        node_color = item.node_color_hex or "#000000"
        return f"width: 0.2rem; align-self: stretch; background: {node_color};"

    @staticmethod
    def _notification_tray_progress_track_style(item: _ModWebNotificationTrayItem) -> str:
        app_border_color = item.app_color_hex or "#000000"
        return f"height: 0.9rem; background: var(--mod-accent-dark); border-top: 1px solid {app_border_color};"

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
                " background: linear-gradient(270deg, var(--mod-accent), var(--mod-accent-dark));"
            )
        return (
            f"position: absolute; top: 0; left: 0; height: 100%; width: {width_percent:.2f}%;"
            " background: linear-gradient(90deg, var(--mod-accent), var(--mod-accent-dark));"
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
            self._guarded_reload(ui=ui)

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
