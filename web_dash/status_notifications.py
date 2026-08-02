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
