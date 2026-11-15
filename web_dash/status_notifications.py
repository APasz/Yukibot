"""StatusNotifications UI helpers."""

from __future__ import annotations

# ruff: noqa: F403, F405
from .status_support import *

if TYPE_CHECKING:
    from nicegui.element import Element


@dataclass(slots=True)
class _TransferOverlayTrack:
    item: _ModWebNotificationTrayItem
    track: Element
    fill: Element


class ModWebStatusNotificationsMixin(ModWebStatusFeatureSupport):
    def _render_user_transfer_overlay(self, *, ui: ModWebUi, user: ModWebUser) -> None:
        tracks: list[_TransferOverlayTrack] = []
        detail_label: Label | None = None

        def _render_items(items: tuple[_ModWebNotificationTrayItem, ...]) -> None:
            nonlocal detail_label
            tracks.clear()
            detail_label = None
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
                        with (
                            ui.element("div")
                            .classes(self._transfer_overlay_track_classes(item))
                            .props(self._transfer_overlay_track_props(item))
                            .style(self._transfer_overlay_track_style(item))
                        ) as track:
                            fill = ui.element("div").classes("mod-transfer-overlay-fill").style(
                                self._transfer_overlay_fill_style(item)
                            )
                            tracks.append(_TransferOverlayTrack(item=item, track=track, fill=fill))
                detail = self._transfer_overlay_detail(items)
                detail_label = ui.label(detail or "").classes("mod-transfer-overlay-detail")
                detail_label.set_visibility(detail is not None)

        refreshable = getattr(ui, "refreshable", None)
        if not callable(refreshable):
            items = self._user_transfer_overlay_items(user=user)
            if items:
                with ui.teleport("body"):
                    _render_items(items)
            return

        @ui.refreshable
        def _overlay() -> None:
            _render_items(self._user_transfer_overlay_items(user=user))

        # A transformed page ancestor would capture fixed positioning, even after its entry animation.
        with ui.teleport("body"):
            _overlay()
        ui_context: object | None = getattr(cast(object, ui), "context", None)
        client: object | None = getattr(ui_context, "client", None) if ui_context is not None else None
        safe_invoke = getattr(client, "safe_invoke", None) if client is not None else None
        if not callable(safe_invoke):
            return
        loop = asyncio.get_running_loop()

        def _update_items() -> None:
            items = self._user_transfer_overlay_items(user=user)
            if len(items) != len(tracks):
                _overlay.refresh()
                return
            for item, binding in zip(items, tracks, strict=True):
                if item == binding.item:
                    continue
                binding.track.classes(replace=self._transfer_overlay_track_classes(item))
                binding.track.props(self._transfer_overlay_track_props(item), remove="aria-valuenow aria-valuetext")
                binding.track.style(self._transfer_overlay_track_style(item))
                binding.fill.style(replace=self._transfer_overlay_fill_style(item))
                binding.item = item
            if detail_label is not None:
                detail = self._transfer_overlay_detail(items)
                detail_label.set_text(detail or "")
                detail_label.set_visibility(detail is not None)

        def _request_overlay_refresh() -> None:
            def _queue_refresh() -> None:
                safe_invoke(_update_items)

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
    def _transfer_overlay_track_classes(item: _ModWebNotificationTrayItem) -> str:
        classes = "mod-transfer-overlay-track"
        if item.blink:
            classes += " animate-pulse"
            if item.progress_percent is None:
                classes += " mod-transfer-overlay-track-indeterminate"
        return classes

    @staticmethod
    def _transfer_overlay_track_props(item: _ModWebNotificationTrayItem) -> str:
        props = f"role=progressbar aria-valuemin=0 aria-valuemax=100 aria-label={json.dumps(item.label, ensure_ascii=False)}"
        if item.progress_percent is None:
            props += f" aria-valuetext={json.dumps(item.detail_text or item.label, ensure_ascii=False)}"
        else:
            props += f" aria-valuenow={item.progress_percent:.2f}"
        return props

    @staticmethod
    def _transfer_overlay_detail(items: tuple[_ModWebNotificationTrayItem, ...]) -> str | None:
        item = next((item for item in items if item.state is ModWebNotificationTrayItemState.ACTIVE), None)
        if item is None:
            return None
        if item.progress_percent is None:
            return item.detail_text or item.label
        return f"{item.label} · {item.progress_percent:.0f}%"

    @staticmethod
    def _transfer_overlay_track_style(item: _ModWebNotificationTrayItem) -> str:
        return (
            f"--mod-transfer-colour: {ModWebStatusNotificationsMixin._transfer_overlay_colour(item)};"
            f" height: {_TRANSFER_OVERLAY_TRACK_HEIGHT_REM:.2f}rem;"
            f" flex: 0 0 {_TRANSFER_OVERLAY_TRACK_HEIGHT_REM:.2f}rem;"
        )

    @staticmethod
    def _transfer_overlay_fill_style(item: _ModWebNotificationTrayItem) -> str:
        edge: str = "right" if item.kind is ModWebNotificationTrayItemKind.UPLOAD else "left"
        width: float = 100.0 if item.progress_percent is None else item.progress_percent
        return (
            f"position: absolute; top: 0; {edge}: 0; height: 100%; width: {width:.2f}%;"
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
