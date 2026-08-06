"""StatusNotifications UI helpers."""

from __future__ import annotations

# ruff: noqa: F403, F405
from .status_support import *


class ModWebStatusNotificationsMixin(ModWebStatusFeatureSupport):
    def _render_user_transfer_overlay(self, *, ui: ModWebUi, user: ModWebUser) -> None:
        def _render_items(items: tuple[_ModWebNotificationTrayItem, ...]) -> None:
            if not items:
                return
            with (
                ui.element("div")
                .classes("mod-transfer-overlay")
                .props("role=status aria-live=polite")
                .style(self._transfer_overlay_container_style())
            ):
                with ui.element("div").classes("mod-transfer-overlay-tracks"):
                    for item in items:
                        track_classes: str = "mod-transfer-overlay-track"
                        if item.blink:
                            track_classes = f"{track_classes} animate-pulse"
                        progress_percent: float = self._transfer_overlay_progress_percent(item)
                        with (
                            ui.element("div")
                            .classes(track_classes)
                            .props(
                                "role=progressbar aria-valuemin=0 aria-valuemax=100 "
                                f"aria-valuenow={progress_percent:.2f}"
                            )
                            .style(self._transfer_overlay_track_style(item))
                        ):
                            ui.element("div").classes("mod-transfer-overlay-fill").style(
                                self._transfer_overlay_fill_style(item=item, progress_percent=progress_percent)
                            )

        refreshable = getattr(ui, "refreshable", None)
        if not callable(refreshable):
            _render_items(self._user_transfer_overlay_items(user=user))
            return

        @ui.refreshable
        def _overlay() -> None:
            _render_items(self._user_transfer_overlay_items(user=user))

        _overlay()
        ui_context: object | None = getattr(cast(object, ui), "context", None)
        client: object | None = getattr(ui_context, "client", None) if ui_context is not None else None
        safe_invoke = getattr(client, "safe_invoke", None) if client is not None else None
        if not callable(safe_invoke):
            return
        loop = asyncio.get_running_loop()

        def _request_overlay_refresh() -> None:
            def _queue_refresh() -> None:
                safe_invoke(_overlay.refresh)

            loop.call_soon_threadsafe(_queue_refresh)

        unsubscribe = self._backend.subscribe_user_transfers(
            user_id=user.discord_id,
            subscriber=_request_overlay_refresh,
        )
        self._register_client_cleanup(ui=ui, cleanup=unsubscribe)

    def _user_transfer_overlay_items(self, *, user: ModWebUser) -> tuple[_ModWebNotificationTrayItem, ...]:
        return self._backend.user_transfer_items(user_id=user.discord_id)

    @staticmethod
    def _transfer_overlay_container_style() -> str:
        return "position: fixed; top: 0; right: 0; left: 0; z-index: 2000; pointer-events: none;"

    @staticmethod
    def _transfer_overlay_progress_percent(item: _ModWebNotificationTrayItem) -> float:
        return item.progress_percent or 0.0

    @staticmethod
    def _transfer_overlay_track_style(item: _ModWebNotificationTrayItem) -> str:
        return (
            f"--mod-transfer-colour: {ModWebStatusNotificationsMixin._transfer_overlay_colour(item)};"
            f" height: {_TRANSFER_OVERLAY_TRACK_HEIGHT_REM:.2f}rem;"
            f" flex: 0 0 {_TRANSFER_OVERLAY_TRACK_HEIGHT_REM:.2f}rem;"
        )

    @staticmethod
    def _transfer_overlay_fill_style(*, item: _ModWebNotificationTrayItem, progress_percent: float) -> str:
        edge: str = "right" if item.kind is ModWebNotificationTrayItemKind.UPLOAD else "left"
        return (
            f"position: absolute; top: 0; {edge}: 0; height: 100%; width: {progress_percent:.2f}%;"
            " background: var(--mod-transfer-colour);"
        )

    @staticmethod
    def _user_header_surface_style() -> str:
        return (
            f"min-height: {_USER_HEADER_SURFACE_MIN_HEIGHT_REM:.2f}rem;"
            "width: 100%;"
            "display: flex; flex-direction: column; justify-content: space-between;"
            "padding: 0.5rem 0.75rem;"
            "box-sizing: border-box;"
            "border: 1px solid rgba(255,255,255,0.08);"
            "background: rgba(0,0,0,0.28);"
            "overflow: hidden;"
        )

    @staticmethod
    def _transfer_overlay_colour(item: _ModWebNotificationTrayItem) -> str:
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
        return item.app_color_hex or item.node_color_hex or "var(--mod-accent)"

    def _user_can_use_fake_chat_preview(self, user: ModWebUser) -> bool:
        return self._user_has_level(user, Power_Level.root)
