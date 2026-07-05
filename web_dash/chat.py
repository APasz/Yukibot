from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from typing import TYPE_CHECKING

from relay_notices import (
    notice_additional_badge_specs,
    notice_badge_spec,
    notice_hides_body_content,
    relay_notice_badge_spec_from_label,
)

from .assets import extract_html_tag_contents
from .constants import (
    _CHAT_GROUP_WINDOW_SECONDS,
    _CHAT_HISTORY_LIMIT,
    _CHAT_HISTORY_REFRESH_INTERVAL_SECONDS,
    _CHAT_MARKUP_BOLD_RE,
    _CHAT_MARKUP_CODE_BLOCK_RE,
    _CHAT_MARKUP_DISCORD_CHANNEL_RE,
    _CHAT_MARKUP_DISCORD_MENTION_RE,
    _CHAT_MARKUP_DISCORD_ROLE_RE,
    _CHAT_MARKUP_DISCORD_TIMESTAMP_RE,
    _CHAT_MARKUP_ESCAPE_RE,
    _CHAT_MARKUP_HEADER_RE,
    _CHAT_MARKUP_INLINE_CODE_RE,
    _CHAT_MARKUP_ITALIC_STAR_RE,
    _CHAT_MARKUP_ITALIC_UNDERSCORE_RE,
    _CHAT_MARKUP_LINK_RE,
    _CHAT_MARKUP_ORDERED_LIST_RE,
    _CHAT_MARKUP_RAW_URL_RE,
    _CHAT_MARKUP_SPOILER_RE,
    _CHAT_MARKUP_STRIKETHROUGH_RE,
    _CHAT_MARKUP_SUBTEXT_RE,
    _CHAT_MARKUP_UNDERLINE_RE,
    _CHAT_MARKUP_UNORDERED_LIST_RE,
    _CHAT_MEDIA_AUDIO_EXTENSIONS,
    _CHAT_MEDIA_IMAGE_EXTENSIONS,
    _CHAT_MEDIA_VIDEO_EXTENSIONS,
    _CHAT_TIMELINE_BOTTOM_THRESHOLD_PX,
    _REMOTE_CHAT_STREAM_HEARTBEAT_SECONDS,
    _REMOTE_CHAT_STREAM_RECONNECT_DELAY_SECONDS,
    _WEB_CHAT_MESSAGE_MAX_LENGTH,
    log,
)
from .json_helpers import _json_object_from_text
from .nicegui_protocols import ModWebUi, _value_as_text
from .runtime_imports import (
    DEFAULT_CHAT_AUTHOR_COLOR_HEX,
    App,
    App_Manager,
    Awaitable,
    BadgeTone,
    Callable,
    ChatAttachment,
    ChatAuthorKind,
    ChatEmbed,
    ChatEndpointKind,
    ChatEvent,
    ChatHub,
    ChatLink,
    ChatMessageReference,
    ChatReferenceKind,
    Html,
    Input,
    Label,
    Mapping,
    ModWebUser,
    NodeApiScope,
    NodeAppEntry,
    NodeAppRuntimeSummary,
    NodeAppTransitionState,
    NodeChatRoomSnapshot,
    NodeChatStreamEvent,
    NodeChatStreamEventKind,
    Power_Level,
    PurePosixPath,
    Request,
    ScrollArea,
    Tooltip,
    aiohttp,
    asyncio,
    cast,
    config,
    escape,
    hashlib,
    mimetypes,
    quote,
    re,
    time,
    urlencode,
    urlsplit,
)
from .service_base import ModWebServiceSupport
from .stream_broker import RemoteChatStreamKey
from .streams import ModWebStreamsMixin
from .types import (
    ChatMediaPreviewKind,
    ModWebBasePageModel,
    ModWebNodeLink,
    _ChatMediaPreview,
    _ModWebBadgeSpec,
    _ModWebChatComposeRequest,
    _ModWebChatEventGroup,
    _ModWebChatPanelConfig,
    _ModWebChatPanelSignal,
    _ModWebChatSurfaceConfig,
    RemoteChatBrokerEvent,
)
from .ui_helpers import ModWebUiHelpersMixin, copy_text_to_clipboard

if TYPE_CHECKING:
    from nicegui.element import Element
    from nicegui.elements.link import Link
    from nicegui.events import ScrollEventArguments

class ModWebChatMixin(ModWebServiceSupport):
    def _local_chat_snapshot(self, room_id: str) -> NodeChatRoomSnapshot:
        return self._node_api.build_chat_room_snapshot(self._resolve_app(room_id), limit=_CHAT_HISTORY_LIMIT)

    def _local_chat_panel_config(
        self,
        *,
        room_id: str,
        session_id: str,
        user: ModWebUser,
        app_scope: str | None,
        include_runtime_updates: bool = True,
    ) -> _ModWebChatPanelConfig:
        app = self._chat_room_app(room_id)
        app_platforms = tuple(str(platform) for platform in getattr(app, "name_platforms", ()))
        preferred_platform = cast(str | None, getattr(app, "preferred_name_platform", None))

        async def _refresh_snapshot() -> NodeChatRoomSnapshot:
            return self._local_chat_snapshot(room_id)

        def _subscribe_updates(on_update: Callable[[_ModWebChatPanelSignal], None]) -> Callable[[], None]:
            room_subscription_id = ChatHub().subscribe(
                room_id,
                lambda update: on_update(
                    _ModWebChatPanelSignal.chat(
                        events=() if update.event is None else (update.event,),
                    )
                ),
            )
            if include_runtime_updates:
                unsubscribe_runtime = self._node_api.subscribe_local_app_runtime(
                    room_id,
                    lambda event: (
                        on_update(_ModWebChatPanelSignal.runtime(app_stats=event.app_stats))
                        if (not event.is_initial and event.app_stats is not None)
                        else None
                    ),
                )
            else:
                unsubscribe_runtime = lambda: None

            def _unsubscribe() -> None:
                ChatHub().unsubscribe(room_id, room_subscription_id)
                unsubscribe_runtime()

            return _unsubscribe

        send_message: Callable[[_ModWebChatComposeRequest], Awaitable[ChatEvent]] | None = None
        if self._chat_relay is not None:
            chat_relay = self._chat_relay

            async def _send_message(request: _ModWebChatComposeRequest) -> ChatEvent:
                return await chat_relay.publish_web_chat(
                    room_id=room_id,
                    session_id=session_id,
                    author_display_name=self._web_chat_author_display_name(
                        user,
                        scope=app_scope,
                        platforms=app_platforms,
                        preferred_platform=preferred_platform,
                    ),
                    author_id=str(user.discord_id),
                    discord_user_id=user.discord_id,
                    content=request.content,
                    reply_to_event_id=request.reply_to_event_id,
                )

            send_message = _send_message

        return _ModWebChatPanelConfig(
            initial_snapshot=self._local_chat_snapshot(room_id),
            refresh_snapshot=_refresh_snapshot,
            send_message=send_message,
            subscribe_updates=_subscribe_updates,
        )

    async def _remote_chat_panel_config(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str,
        session_id: str,
        user: ModWebUser,
        app_scope: str | None,
        include_runtime_updates: bool = True,
    ) -> _ModWebChatPanelConfig:
        app = self._chat_room_app(app_name)
        app_platforms = tuple(str(platform) for platform in getattr(app, "name_platforms", ()))
        preferred_platform = cast(str | None, getattr(app, "preferred_name_platform", None))

        async def _refresh_snapshot() -> NodeChatRoomSnapshot:
            return await self._remote_chat_snapshot_async(node, app_name, user)

        async def _send_message(request: _ModWebChatComposeRequest) -> ChatEvent:
            return await self._remote_publish_web_chat_async(
                node=node,
                app_name=app_name,
                session_id=session_id,
                user=user,
                request=request,
                author_display_name=self._web_chat_author_display_name(
                    user,
                    scope=app_scope,
                    platforms=app_platforms,
                    preferred_platform=preferred_platform,
                ),
            )

        initial_snapshot = await _refresh_snapshot()

        def _subscribe_updates(on_update: Callable[[_ModWebChatPanelSignal], None]) -> Callable[[], None]:
            stream_healthy = False
            fallback_wakeup = asyncio.Event()

            def _set_stream_health(healthy: bool) -> None:
                nonlocal stream_healthy
                if stream_healthy == healthy:
                    return
                stream_healthy = healthy
                fallback_wakeup.set()

            key = RemoteChatStreamKey(node=node, app_name=app_name.casefold())

            def _apply_broker_event(event: RemoteChatBrokerEvent) -> None:
                if event.stream_healthy is not None:
                    _set_stream_health(event.stream_healthy)
                signal = event.signal
                if signal is None:
                    return
                if include_runtime_updates:
                    on_update(signal)
                elif signal.chat_changed:
                    on_update(
                        _ModWebChatPanelSignal.chat(
                            snapshot=signal.snapshot,
                            events=signal.events,
                        )
                    )

            async def _listen(publish: Callable[[RemoteChatBrokerEvent], None]) -> None:
                await self._remote_chat_stream_listener(
                    node=node,
                    app_name=app_name,
                    user=user,
                    on_update=lambda signal: publish(
                        RemoteChatBrokerEvent(signal=signal, stream_healthy=True)
                    ),
                    include_runtime_updates=True,
                    on_stream_health_change=lambda healthy: publish(
                        RemoteChatBrokerEvent(stream_healthy=healthy)
                    ),
                    after_revision=initial_snapshot.revision,
                )
                await asyncio.Event().wait()

            unsubscribe_stream = self._remote_chat_broker.subscribe(
                key=key,
                callback=_apply_broker_event,
                listener_factory=_listen,
                replay_latest=True,
            )
            fallback_task = asyncio.create_task(
                self._chat_stream_fallback_loop(
                    fallback_signal=(
                        _ModWebChatPanelSignal.both()
                        if include_runtime_updates
                        else _ModWebChatPanelSignal.chat()
                    ),
                    is_stream_healthy=lambda: stream_healthy,
                    on_update=on_update,
                    wakeup=fallback_wakeup,
                    refresh_interval_seconds=_CHAT_HISTORY_REFRESH_INTERVAL_SECONDS,
                )
            )

            def _unsubscribe() -> None:
                unsubscribe_stream()
                fallback_task.cancel()
                fallback_wakeup.set()

            return _unsubscribe

        return _ModWebChatPanelConfig(
            initial_snapshot=initial_snapshot,
            refresh_snapshot=_refresh_snapshot,
            send_message=_send_message,
            subscribe_updates=_subscribe_updates,
        )

    async def _local_chat_surface_config(
        self,
        *,
        app: App,
        request: Request,
        user: ModWebUser,
        app_stats: NodeAppRuntimeSummary | None = None,
        include_runtime_updates: bool = True,
    ) -> _ModWebChatSurfaceConfig:
        if not app.supports_chat_relay:
            raise ValueError(f"{app.friendly} does not expose a chat relay.")

        session_id = self._web_chat_session_id(app_name=app.name, user=user, request=request)
        initial_app_stats = app_stats
        if initial_app_stats is None:
            initial_app_stats = await self._node_api.build_live_app_runtime_summary(app)

        async def _refresh_app_stats() -> NodeAppRuntimeSummary | None:
            return await self._node_api.build_live_app_runtime_summary(app)

        return _ModWebChatSurfaceConfig(
            panel=self._local_chat_panel_config(
                room_id=app.name,
                session_id=session_id,
                user=user,
                app_scope=app.scope,
                include_runtime_updates=include_runtime_updates,
            ),
            node_name=config.MOD_WEB_SERVER.node_name,
            app_friendly=app.friendly,
            app_color_hex=self._node_api.app_color_hex(app.manage_embed_color),
            app_stats=initial_app_stats,
            hero_badges=(_ModWebBadgeSpec(text=app.chat_relay_support.display_value, tone="purple"),),
            refresh_app_stats=_refresh_app_stats if include_runtime_updates else None,
            popout_url=self.app_chat_path(app.name),
            map_url=app.public_map_url,
        )

    async def _remote_chat_surface_config(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str,
        request: Request,
        user: ModWebUser,
        app_entry: NodeAppEntry | None = None,
        app_stats: NodeAppRuntimeSummary | None = None,
        include_runtime_updates: bool = True,
    ) -> _ModWebChatSurfaceConfig:
        resolved_app_entry = (
            app_entry
            if app_entry is not None
            else await self._remote_app_entry_async(node, app_name, user)
        )
        resolved_app_color_hex = self._resolved_app_color_hex(
            app_name=resolved_app_entry.name,
            scope=resolved_app_entry.scope,
            color_hex=resolved_app_entry.color_hex,
        )
        if not resolved_app_entry.supports_chat:
            raise ValueError(f"{resolved_app_entry.friendly} does not expose a chat relay.")

        session_id = self._web_chat_session_id(app_name=app_name, user=user, request=request)
        panel = await self._remote_chat_panel_config(
            node=node,
            app_name=app_name,
            session_id=session_id,
            user=user,
            app_scope=resolved_app_entry.scope,
            include_runtime_updates=include_runtime_updates,
        )
        initial_app_stats = app_stats
        if initial_app_stats is None:
            initial_app_stats = await self._remote_app_runtime_summary_async(node, app_name, user)

        async def _refresh_app_stats() -> NodeAppRuntimeSummary | None:
            return await self._remote_app_runtime_summary_async(node, app_name, user)

        return _ModWebChatSurfaceConfig(
            panel=panel,
            node_name=node.node_name,
            app_friendly=resolved_app_entry.friendly,
            app_color_hex=resolved_app_color_hex,
            app_stats=initial_app_stats,
            hero_badges=(_ModWebBadgeSpec(text=initial_app_stats.relay_support.display_value, tone="grey"),),
            refresh_app_stats=_refresh_app_stats if include_runtime_updates else None,
            popout_url=self.node_app_chat_path(node.node_name, resolved_app_entry.name),
            map_url=resolved_app_entry.map_url,
        )

    async def _remote_chat_snapshot_async(
        self,
        node: ModWebNodeLink,
        app_name: str,
        user: ModWebUser,
    ) -> NodeChatRoomSnapshot:
        payload = await self._remote_json_async(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/chat?{urlencode({'limit': _CHAT_HISTORY_LIMIT})}",
            scopes=(NodeApiScope.CHAT_READ,),
            user=user,
        )
        return NodeChatRoomSnapshot.from_mapping(payload)

    async def _remote_chat_stream_listener(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str,
        user: ModWebUser,
        on_update: Callable[[_ModWebChatPanelSignal], None],
        include_runtime_updates: bool = True,
        on_stream_health_change: Callable[[bool], None] | None = None,
        after_revision: int | None = None,
    ) -> None:
        resume_revision = after_revision
        while True:
            stream_healthy = False
            try:
                token = self._remote_token(
                    node=node,
                    app_name=app_name,
                    scopes=(NodeApiScope.CHAT_READ, NodeApiScope.MODS_READ),
                    user=user,
                )
                session = await self._remote_http_client()
                async with session.ws_connect(
                    self._remote_chat_stream_url(
                        node=node,
                        app_name=app_name,
                        after_revision=resume_revision,
                    ),
                    headers={"Authorization": f"Bearer {token}"},
                    heartbeat=_REMOTE_CHAT_STREAM_HEARTBEAT_SECONDS,
                ) as websocket:
                    stream_healthy = True
                    if on_stream_health_change is not None:
                        on_stream_health_change(True)
                    async for message in websocket:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            payload_text: object = cast(object, message.data)
                            payload = _json_object_from_text(
                                payload_text,
                                context="Remote chat stream message",
                            )
                            event = NodeChatStreamEvent.from_mapping(payload)
                            resume_revision = max(resume_revision or 0, event.revision)
                            if event.room_id.casefold() != app_name.casefold():
                                raise RuntimeError(
                                    "Remote chat stream room id mismatch: "
                                    f"expected={app_name!r} got={event.room_id!r}"
                                )
                            signal = self._remote_chat_stream_signal(
                                event,
                                include_runtime_updates=include_runtime_updates,
                            )
                            if signal is not None:
                                on_update(signal)
                            continue
                        if message.type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                        }:
                            break
                        if message.type == aiohttp.WSMsgType.ERROR:
                            raise RuntimeError(f"Remote chat stream websocket error: {websocket.exception()}")
            except asyncio.CancelledError:
                raise
            except Exception as xcp:
                if ModWebStreamsMixin._remote_websocket_stream_is_unsupported(xcp):
                    log.warning(
                        "Remote chat stream websocket unsupported: node=%s app=%s status=%s; falling back to polling",
                        node.node_name,
                        app_name,
                        getattr(xcp, "status", None),
                    )
                    return
                log.warning(
                    "Remote chat stream failed: node=%s app=%s error=%s",
                    node.node_name,
                    app_name,
                    xcp,
                )
            finally:
                if stream_healthy and on_stream_health_change is not None:
                    on_stream_health_change(False)
            await asyncio.sleep(_REMOTE_CHAT_STREAM_RECONNECT_DELAY_SECONDS)

    @staticmethod
    async def _chat_stream_fallback_loop(
        *,
        fallback_signal: _ModWebChatPanelSignal,
        is_stream_healthy: Callable[[], bool],
        on_update: Callable[[_ModWebChatPanelSignal], None],
        wakeup: asyncio.Event,
        refresh_interval_seconds: float,
    ) -> None:
        while True:
            if is_stream_healthy():
                await wakeup.wait()
                wakeup.clear()
                continue
            try:
                await asyncio.wait_for(wakeup.wait(), timeout=refresh_interval_seconds)
            except asyncio.TimeoutError:
                on_update(fallback_signal)
            finally:
                wakeup.clear()

    @staticmethod
    def _remote_chat_stream_signal(
        event: NodeChatStreamEvent,
        *,
        include_runtime_updates: bool = True,
    ) -> _ModWebChatPanelSignal | None:
        chat_changed = bool(event.events) or event.snapshot is not None or event.kind is NodeChatStreamEventKind.CHAT_CHANGED
        runtime_changed = include_runtime_updates and (
            event.app_stats is not None
            or event.kind
            in {
                NodeChatStreamEventKind.INITIAL,
                NodeChatStreamEventKind.RUNTIME_CHANGED,
            }
        )
        if not chat_changed and not runtime_changed:
            return None
        return _ModWebChatPanelSignal(
            chat_changed=chat_changed,
            runtime_changed=runtime_changed,
            snapshot=event.snapshot,
            app_stats=event.app_stats if include_runtime_updates else None,
            events=event.events,
        )

    @staticmethod
    def _remote_chat_stream_url(
        *,
        node: ModWebNodeLink,
        app_name: str,
        after_revision: int | None = None,
    ) -> str:
        url = ModWebStreamsMixin._remote_websocket_url(
            node=node,
            path=f"/apps/{quote(app_name, safe='')}/chat/stream",
        )
        if after_revision is None:
            return url
        return f"{url}?{urlencode({'after_revision': after_revision})}"

    async def _remote_publish_web_chat_async(
        self,
        node: ModWebNodeLink,
        app_name: str,
        session_id: str,
        user: ModWebUser,
        request: _ModWebChatComposeRequest,
        author_display_name: str,
    ) -> ChatEvent:
        payload = await self._remote_json_async(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/chat",
            scopes=(NodeApiScope.CHAT_WRITE,),
            user=user,
            method="POST",
            json_payload={
                "session_id": session_id,
                "author_display_name": author_display_name,
                "content": request.content,
                "reply_to_event_id": request.reply_to_event_id,
            },
        )
        return ChatEvent.from_mapping(payload)

    async def _render_chat_page(self, *, ui: ModWebUi, app_name: str, request: Request) -> None:
        await self._render_remote_chat_page(
            ui=ui,
            node_name=self._default_mod_web_node_name(),
            app_name=app_name,
            request=request,
        )

    async def _render_remote_chat_page(self, *, ui: ModWebUi, node_name: str, app_name: str, request: Request) -> None:
        user = await self._authorised_page_user(ui=ui, request=request, required_level=Power_Level.visitor)
        if user is None:
            return
        try:
            node = self._remote_node_link(node_name)
            app_entry = await self._remote_app_entry_async(node, app_name, user)
            chat_surface = await self._remote_chat_surface_config(
                node=node,
                app_name=app_name,
                request=request,
                user=user,
                app_entry=app_entry,
            )
        except ValueError as xcp:
            self._render_error_page(ui=ui, title="Chat unavailable", detail=str(xcp), app_name=app_name)
            return
        except Exception as xcp:
            log.exception("Remote mod web chat page render failed: node=%s app=%s", node_name, app_name)
            self._render_error_page(ui=ui, title="Chat unavailable", detail=str(xcp), app_name=app_name)
            return

        self._apply_theme(ui=ui)

        with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
            self._render_user_header(ui=ui, user=user)
            self._render_chat_page_card(ui=ui, chat_surface=chat_surface)

    def _render_chat_section(
        self,
        *,
        ui: ModWebUi,
        chat_surface: _ModWebChatSurfaceConfig,
        endpoint_count_label: Label | None = None,
        endpoint_count_tooltip: "Tooltip | None" = None,
        endpoint_count_tooltip_content: Html | None = None,
    ) -> Callable[[ModWebBasePageModel], None]:
        with ui.column().classes("w-full min-h-0"):
            apply_runtime_stats = self._render_chat_panel(
                ui=ui,
                chat_panel=chat_surface.panel,
                app_friendly=chat_surface.app_friendly,
                app_stats=chat_surface.app_stats,
                refresh_app_stats=None,
                show_header=False,
                endpoint_count_label=endpoint_count_label,
                endpoint_count_tooltip=endpoint_count_tooltip,
                endpoint_count_tooltip_content=endpoint_count_tooltip_content,
                embedded=True,
            )

        def apply_runtime_model(runtime_model: ModWebBasePageModel) -> None:
            apply_runtime_stats(runtime_model.app_stats)

        return apply_runtime_model

    def _render_chat_page_card(
        self,
        *,
        ui: ModWebUi,
        chat_surface: _ModWebChatSurfaceConfig,
    ) -> None:
        can_send = chat_surface.panel.send_message is not None
        app_status_label, app_status_tone = self._chat_app_status_badge(chat_surface.app_stats)
        player_count_badge = self._chat_player_count_badge(chat_surface.app_stats)
        player_count_tooltip_html: str | None = self._player_count_tooltip_html(
            connected_player_names=chat_surface.app_stats.connected_player_names
            if chat_surface.app_stats is not None
            else (),
            fallback_text=player_count_badge.text if player_count_badge is not None else None,
        )
        with (
            ui.card()
            .classes(f"{self._hero_card_classes()} mod-chat-shell-card")
            .style(self._hero_card_style(chat_surface.app_color_hex))
        ):
            self._render_app_node_badge(ui=ui, node_name=chat_surface.node_name)
            with ui.column().classes(f"{self._hero_shell_classes()} mod-chat-shell"):
                with ui.row().classes("mod-chat-shell-header w-full items-center justify-between gap-3"):
                    with ui.column().classes("mod-chat-shell-header-main gap-1"):
                        ui.label(f"{chat_surface.app_friendly}").classes(self._hero_title_classes())
                    with ui.column().classes(self._hero_badges_classes(wide=True)):
                        with ui.row().classes(self._hero_badge_row_classes()):
                            for badge in chat_surface.hero_badges:
                                self._badge(ui=ui, text=badge.text, tone=badge.tone)
                            if chat_surface.map_url is not None:
                                self._badge_link(
                                    ui=ui,
                                    text="Map",
                                    tone="purple",
                                    url=chat_surface.map_url,
                                    new_tab=True,
                                )
                            (
                                endpoint_count_label,
                                endpoint_count_tooltip,
                                endpoint_count_tooltip_content,
                            ) = self._render_chat_endpoint_badge(
                                ui=ui,
                                snapshot=chat_surface.panel.initial_snapshot,
                            )
                            self._badge(ui=ui, text="Live", tone="purple" if can_send else "warn")
                            app_status_badge_label = self._badge(
                                ui=ui,
                                text=app_status_label,
                                tone=app_status_tone,
                            )
                            player_count_badge_label = self._badge(
                                ui=ui,
                                text=player_count_badge.text if player_count_badge is not None else "",
                                tone=player_count_badge.tone if player_count_badge is not None else "grey",
                            )
                            player_count_tooltip, player_count_tooltip_content = self._attach_html_tooltip(
                                ui=ui,
                                target=player_count_badge_label,
                                html=player_count_tooltip_html or "",
                            )
                            self._set_optional_badge_state(
                                player_count_badge_label,
                                player_count_badge,
                            )
                self._render_chat_panel(
                    ui=ui,
                    chat_panel=chat_surface.panel,
                    app_friendly=chat_surface.app_friendly,
                    app_stats=chat_surface.app_stats,
                    refresh_app_stats=chat_surface.refresh_app_stats,
                    show_header=False,
                    endpoint_count_label=endpoint_count_label,
                    endpoint_count_tooltip=endpoint_count_tooltip,
                    endpoint_count_tooltip_content=endpoint_count_tooltip_content,
                    app_status_badge_label=app_status_badge_label,
                    player_count_badge_label=player_count_badge_label,
                    player_count_tooltip=player_count_tooltip,
                    player_count_tooltip_content=player_count_tooltip_content,
                    embedded=True,
                )

    def _render_chat_panel(
        self,
        *,
        ui: ModWebUi,
        chat_panel: _ModWebChatPanelConfig,
        app_friendly: str,
        app_stats: NodeAppRuntimeSummary | None,
        refresh_app_stats: Callable[[], Awaitable[NodeAppRuntimeSummary | None]] | None,
        show_header: bool = True,
        header_badges: tuple[_ModWebBadgeSpec, ...] = (),
        endpoint_count_label: Label | None = None,
        endpoint_count_tooltip: "Tooltip | None" = None,
        endpoint_count_tooltip_content: Html | None = None,
        app_status_badge_label: Label | None = None,
        player_count_badge_label: Label | None = None,
        player_count_tooltip: "Tooltip | None" = None,
        player_count_tooltip_content: Html | None = None,
        popout_url: str | None = None,
        embedded: bool = False,
    ) -> Callable[[NodeAppRuntimeSummary | None], None]:
        can_send = chat_panel.send_message is not None
        app_status_label, app_status_tone = self._chat_app_status_badge(app_stats)
        player_count_badge = self._chat_player_count_badge(app_stats)
        message_input: Input | None = None
        message_count_label: Label | None = None
        reply_reference: ChatMessageReference | None = None
        reply_reference_source_guild_id: int | None = None
        reply_to_event_id: str | None = None
        endpoint_count_display = endpoint_count_label
        endpoint_count_tooltip_display = endpoint_count_tooltip
        endpoint_count_tooltip_content_display = endpoint_count_tooltip_content
        player_count_tooltip_display = player_count_tooltip
        player_count_tooltip_content_display = player_count_tooltip_content
        initial_snapshot = chat_panel.initial_snapshot
        chat_scroll_area: ScrollArea | None = None
        chat_timeline: Element | None = None
        chat_near_bottom = True
        last_chat_signature: tuple[str, ...] = self._chat_history_signature(initial_snapshot.events)
        current_chat_events: list[ChatEvent] = list(chat_panel.initial_snapshot.events)
        rendered_chat_groups: list[tuple[tuple[str, ...], Element]] = []
        page_closed = False
        refresh_in_flight = False
        runtime_refresh_in_flight = False
        pending_chat_refresh = False
        pending_runtime_refresh = False
        pending_snapshot: NodeChatRoomSnapshot | None = None
        pending_events: list[ChatEvent] = []
        pending_runtime_payload: NodeAppRuntimeSummary | None = None
        runtime_payload_pending = False
        push_refresh_running = False
        self._ensure_chat_client_script(ui)

        def apply_runtime_stats(next_app_stats: NodeAppRuntimeSummary | None) -> None:
            nonlocal app_stats
            app_stats = next_app_stats
            if app_status_badge_label is not None:
                latest_status_label, latest_status_tone = self._chat_app_status_badge(next_app_stats)
                self._set_badge_state(app_status_badge_label, latest_status_label, latest_status_tone)
            if player_count_badge_label is not None:
                next_player_count_badge = self._chat_player_count_badge(next_app_stats)
                player_count_tooltip_html = self._player_count_tooltip_html(
                    connected_player_names=next_app_stats.connected_player_names if next_app_stats is not None else (),
                    fallback_text=(
                        next_player_count_badge.text if next_player_count_badge is not None else None
                    ),
                )
                self._set_optional_badge_state(
                    player_count_badge_label,
                    next_player_count_badge,
                )
                if player_count_tooltip_display is not None and player_count_tooltip_content_display is not None:
                    self._set_html_tooltip_state(
                        player_count_tooltip_display,
                        player_count_tooltip_content_display,
                        player_count_tooltip_html or "",
                    )

        def update_chat_scroll_position(event: "ScrollEventArguments") -> None:
            nonlocal chat_near_bottom
            if event.vertical_size <= event.vertical_container_size + 1:
                chat_near_bottom = True
                return
            chat_near_bottom = event.vertical_percentage >= 0.97

        async def scroll_chat_to_bottom() -> None:
            if page_closed or chat_scroll_area is None:
                return
            await asyncio.sleep(0)
            chat_scroll_area.scroll_to(percent=1e6)
            await asyncio.sleep(0.05)
            if not page_closed:
                chat_scroll_area.scroll_to(percent=1e6)

        def queue_chat_scroll_to_bottom() -> None:
            asyncio.create_task(scroll_chat_to_bottom())

        def clear_reply_target() -> None:
            nonlocal reply_reference, reply_reference_source_guild_id, reply_to_event_id
            reply_reference = None
            reply_reference_source_guild_id = None
            reply_to_event_id = None
            _reply_banner.refresh()

        def set_reply_target(event: ChatEvent) -> None:
            nonlocal reply_reference, reply_reference_source_guild_id, reply_to_event_id
            reply_reference = self._chat_event_reference(event)
            reply_reference_source_guild_id = event.source_guild_id
            reply_to_event_id = event.id
            _reply_banner.refresh()

        @ui.refreshable
        def _reply_banner() -> None:
            if reply_reference is None:
                return
            with ui.row().classes("mod-chat-reply-banner w-full items-start justify-between gap-3"):
                with ui.column().classes("mod-chat-reply-copy min-w-0 gap-1"):
                    reply_author_name = self._chat_reference_author_display_name(
                        reply_reference,
                        room_id=chat_panel.initial_snapshot.room_id,
                        preferred_guild_id=reply_reference_source_guild_id,
                    )
                    ui.label(f"Replying to {reply_author_name}").classes("mod-chat-reply-label")
                    self._render_chat_markup(
                        ui=ui,
                        text=reply_reference.content,
                        classes="mod-chat-reply-text mod-chat-markup break-words",
                        room_id=chat_panel.initial_snapshot.room_id,
                        preferred_guild_id=reply_reference_source_guild_id,
                    )
                ui.button("Clear", on_click=clear_reply_target).props("flat dense no-caps").classes(
                    "mod-chat-reply-clear"
                )

        @ui.refreshable
        def _chat_messages(events: tuple[ChatEvent, ...]) -> None:
            rendered_chat_groups.clear()
            if not events:
                with ui.column().classes("mod-chat-empty w-full"):
                    ui.label("Relay quiet").classes("text-lg font-black mod-title-small")
                    ui.label("Game, Discord, and web messages will appear here as soon as they move.").classes(
                        "text-sm mod-subtitle"
                    )
                return
            for event_group in self._chat_event_groups(events):
                group_root = self._render_chat_event_group(
                    ui=ui,
                    group=event_group,
                    room_id=chat_panel.initial_snapshot.room_id,
                    can_reply=can_send,
                    on_reply=set_reply_target,
                )
                rendered_chat_groups.append((tuple(event.id for event in event_group.events), group_root))

        async def append_chat_events(events: tuple[ChatEvent, ...], *, force_scroll: bool) -> None:
            nonlocal chat_near_bottom, current_chat_events, last_chat_signature
            if not events or chat_timeline is None:
                return
            existing_event_ids = {event.id for event in current_chat_events}
            new_events = tuple(event for event in events if event.id not in existing_event_ids)
            if not new_events:
                return
            should_scroll_after_refresh = force_scroll or chat_near_bottom
            if not current_chat_events:
                current_chat_events.extend(new_events)
                last_chat_signature = self._chat_history_signature(tuple(current_chat_events))
                _chat_messages.refresh(tuple(current_chat_events))
                if should_scroll_after_refresh:
                    await scroll_chat_to_bottom()
                    chat_near_bottom = True
                return
            with chat_timeline:
                for event in new_events:
                    group = _ModWebChatEventGroup(head_event=event, events=(event,))
                    group_root = self._render_chat_event_group(
                        ui=ui,
                        group=group,
                        room_id=chat_panel.initial_snapshot.room_id,
                        can_reply=can_send,
                        on_reply=set_reply_target,
                    )
                    rendered_chat_groups.append(((event.id,), group_root))
            current_chat_events.extend(new_events)
            while len(current_chat_events) > _CHAT_HISTORY_LIMIT and rendered_chat_groups:
                removed_event_ids, removed_root = rendered_chat_groups.pop(0)
                removed_root.delete()
                removed_event_id_set = set(removed_event_ids)
                current_chat_events = [
                    event for event in current_chat_events if event.id not in removed_event_id_set
                ]
            last_chat_signature = self._chat_history_signature(tuple(current_chat_events))
            if should_scroll_after_refresh:
                await scroll_chat_to_bottom()
                chat_near_bottom = True

        async def refresh_chat_messages(
            *,
            refresh_snapshot: bool = True,
            refresh_runtime: bool = True,
            snapshot_override: NodeChatRoomSnapshot | None = None,
            runtime_override: NodeAppRuntimeSummary | None = None,
            use_runtime_override: bool = False,
            event_overrides: tuple[ChatEvent, ...] = (),
            force_scroll: bool = False,
        ) -> None:
            nonlocal app_stats, chat_near_bottom, current_chat_events, last_chat_signature
            nonlocal refresh_in_flight, runtime_refresh_in_flight
            if page_closed:
                return
            snapshot: NodeChatRoomSnapshot | None = None
            if snapshot_override is not None:
                snapshot = snapshot_override
            elif event_overrides:
                snapshot = None
            elif refresh_snapshot and not refresh_in_flight:
                refresh_in_flight = True
                try:
                    snapshot = await chat_panel.refresh_snapshot()
                except Exception as xcp:
                    log.warning(
                        "Mod web chat refresh failed: app=%s error=%s",
                        chat_panel.initial_snapshot.room_id,
                        xcp,
                    )
                finally:
                    refresh_in_flight = False
            if use_runtime_override:
                apply_runtime_stats(runtime_override)
            elif refresh_runtime and refresh_app_stats is not None and not runtime_refresh_in_flight:
                runtime_refresh_in_flight = True
                try:
                    latest_app_stats = await refresh_app_stats()
                except Exception as xcp:
                    log.warning(
                        "Mod web chat runtime refresh failed: app=%s error=%s",
                        chat_panel.initial_snapshot.room_id,
                        xcp,
                    )
                else:
                    apply_runtime_stats(latest_app_stats)
                finally:
                    runtime_refresh_in_flight = False
            if snapshot is None:
                await append_chat_events(event_overrides, force_scroll=force_scroll)
                return
            if (
                endpoint_count_display is not None
                and endpoint_count_tooltip_display is not None
                and endpoint_count_tooltip_content_display is not None
            ):
                self._set_chat_endpoint_badge_state(
                    endpoint_count_display,
                    endpoint_count_tooltip_display,
                    endpoint_count_tooltip_content_display,
                    snapshot,
                )
            events = snapshot.events
            previous_signature = last_chat_signature
            next_signature = self._chat_history_signature(events)
            changed = next_signature != previous_signature
            if changed:
                should_scroll_after_refresh = force_scroll or chat_near_bottom
                current_chat_events = list(events)
                last_chat_signature = next_signature
                _chat_messages.refresh(events)
                if should_scroll_after_refresh:
                    await scroll_chat_to_bottom()
                    chat_near_bottom = True
            elif force_scroll:
                await scroll_chat_to_bottom()
                chat_near_bottom = True
            await append_chat_events(event_overrides, force_scroll=force_scroll)

        loop = asyncio.get_running_loop()

        async def _drain_push_refresh_queue() -> None:
            nonlocal pending_chat_refresh
            nonlocal pending_runtime_refresh
            nonlocal pending_snapshot
            nonlocal pending_events
            nonlocal pending_runtime_payload
            nonlocal runtime_payload_pending
            nonlocal push_refresh_running
            if push_refresh_running or page_closed:
                return
            push_refresh_running = True
            try:
                while (pending_chat_refresh or pending_runtime_refresh) and not page_closed:
                    refresh_snapshot = pending_chat_refresh
                    refresh_runtime = pending_runtime_refresh
                    snapshot_override = pending_snapshot
                    event_overrides = tuple(pending_events)
                    runtime_override = pending_runtime_payload
                    use_runtime_override = runtime_payload_pending
                    pending_chat_refresh = False
                    pending_runtime_refresh = False
                    pending_snapshot = None
                    pending_events.clear()
                    pending_runtime_payload = None
                    runtime_payload_pending = False
                    await refresh_chat_messages(
                        refresh_snapshot=refresh_snapshot,
                        refresh_runtime=refresh_runtime,
                        snapshot_override=snapshot_override,
                        event_overrides=event_overrides,
                        runtime_override=runtime_override,
                        use_runtime_override=use_runtime_override,
                    )
            finally:
                push_refresh_running = False

        def request_push_refresh(signal: _ModWebChatPanelSignal) -> None:
            def _queue_refresh() -> None:
                nonlocal pending_chat_refresh, pending_runtime_refresh, pending_snapshot, pending_events, pending_runtime_payload
                nonlocal runtime_payload_pending
                if page_closed:
                    return
                pending_chat_refresh = pending_chat_refresh or signal.chat_changed
                pending_runtime_refresh = pending_runtime_refresh or signal.runtime_changed
                if signal.snapshot is not None:
                    pending_snapshot = signal.snapshot
                    pending_events.clear()
                pending_events.extend(signal.events)
                if signal.app_stats is not None:
                    pending_runtime_payload = signal.app_stats
                    runtime_payload_pending = True
                asyncio.create_task(_drain_push_refresh_queue())

            loop.call_soon_threadsafe(_queue_refresh)

        async def send_chat_message() -> None:
            if message_input is None:
                raise RuntimeError("Chat input is not available.")
            raw_content = _value_as_text(message_input)
            content = raw_content.strip()
            if not content:
                ui.notify("Enter a chat message first.", type="warning")
                return
            if len(content) > _WEB_CHAT_MESSAGE_MAX_LENGTH:
                ui.notify(f"Chat messages are limited to {_WEB_CHAT_MESSAGE_MAX_LENGTH} characters.", type="warning")
                return
            if chat_panel.send_message is None:
                ui.notify("Chat relay is not available on this node.", type="negative")
                return
            try:
                sent_event = await chat_panel.send_message(
                    _ModWebChatComposeRequest(content=content, reply_to_event_id=reply_to_event_id)
                )
            except Exception as xcp:
                log.warning("Web chat send failed: app=%s error=%s", chat_panel.initial_snapshot.room_id, xcp)
                ui.notify(f"Chat send failed: {xcp}", type="negative")
                return
            message_input.set_value("")
            clear_reply_target()
            update_message_count()
            await refresh_chat_messages(
                refresh_snapshot=False,
                refresh_runtime=False,
                event_overrides=(sent_event,),
                force_scroll=True,
            )

        def update_message_count() -> None:
            if message_input is None or message_count_label is None:
                return
            message_count_label.set_text(f"{len(_value_as_text(message_input))} / {_WEB_CHAT_MESSAGE_MAX_LENGTH}")

        panel_classes = "mod-chat-panel w-full"
        if embedded:
            panel_classes = f"{panel_classes} mod-chat-panel-embedded"
        with ui.column().classes(panel_classes):
            if show_header:
                with ui.column().classes("mod-chat-header w-full"):
                    with ui.row().classes("mod-chat-header-top w-full items-start justify-between gap-4 flex-wrap"):
                        with ui.column().classes("mod-chat-header-main min-w-0 gap-1"):
                            ui.label(f"{app_friendly}").classes("mod-chat-title mod-title-small")
                        with ui.row().classes("mod-chat-status-row items-center justify-end gap-2 flex-wrap"):
                            for badge in header_badges:
                                self._badge(ui=ui, text=badge.text, tone=badge.tone)
                            (
                                endpoint_count_display,
                                endpoint_count_tooltip_display,
                                endpoint_count_tooltip_content_display,
                            ) = self._render_chat_endpoint_badge(
                                ui=ui,
                                snapshot=initial_snapshot,
                            )
                            self._badge(ui=ui, text="Live", tone="purple" if can_send else "warn")
                            app_status_badge_label = self._badge(
                                ui=ui,
                                text=app_status_label,
                                tone=app_status_tone,
                            )
                            player_count_badge_label = self._badge(
                                ui=ui,
                                text=player_count_badge.text if player_count_badge is not None else "",
                                tone=player_count_badge.tone if player_count_badge is not None else "grey",
                            )
                            player_count_tooltip_display, player_count_tooltip_content_display = (
                                self._attach_html_tooltip(
                                    ui=ui,
                                    target=player_count_badge_label,
                                    html=self._player_count_tooltip_html(
                                        connected_player_names=app_stats.connected_player_names
                                        if app_stats is not None
                                        else (),
                                        fallback_text=(
                                            player_count_badge.text
                                            if player_count_badge is not None
                                            else None
                                        ),
                                    )
                                    or "",
                                )
                            )
                            self._set_optional_badge_state(player_count_badge_label, player_count_badge)
                            if popout_url is not None:
                                self._action_link(
                                    ui=ui,
                                    label="Pop Out",
                                    url=popout_url,
                                    compact=True,
                                    extra_classes="mod-action-border-accent",
                                    new_tab=True,
                                )
            initial_events = initial_snapshot.events
            last_chat_signature = self._chat_history_signature(initial_events)
            with ui.column().classes("mod-chat-timeline-shell w-full"):
                chat_scroll_area = ui.scroll_area(on_scroll=update_chat_scroll_position).classes(
                    "mod-chat-scroll-area w-full"
                )
                with chat_scroll_area:
                    with ui.column().classes("mod-chat-timeline w-full") as chat_timeline:
                        _chat_messages(initial_events)
            initial_scroll_timer = ui.timer(0.1, queue_chat_scroll_to_bottom, once=True)
            self._register_timer_cleanup(ui=ui, timer=initial_scroll_timer)
            with ui.column().classes("mod-chat-composer w-full"):
                with ui.column().classes("mod-chat-composer-surface w-full"):
                    _reply_banner()
                    with ui.row().classes("mod-chat-composer-row w-full items-stretch gap-2 flex-wrap"):
                        message_input = (
                            ui.input(placeholder="Write to the relay")
                            .props(f"filled square dense clearable maxlength={_WEB_CHAT_MESSAGE_MAX_LENGTH}")
                            .classes("mod-chat-input grow")
                        )
                        message_input.on_value_change(update_message_count)
                        message_input.on(
                            "keydown.enter",
                            send_chat_message,
                            js_handler="(event) => { event.preventDefault(); emit(); }",
                        )
                        send_button = ui.button(on_click=send_chat_message).classes("mod-list-button mod-chat-send")
                        with send_button:
                            with ui.column().classes("mod-chat-send-stack"):
                                ui.label("Send").classes("mod-chat-send-label")
                                message_count_label = ui.label(f"0 / {_WEB_CHAT_MESSAGE_MAX_LENGTH}").classes(
                                    "mod-chat-send-subtext"
                                )
                        if not can_send:
                            send_button.disable()
                if not can_send:
                    ui.label("Chat relay delivery is not available on this node.").classes(
                        "mod-chat-composer-warning mod-subtitle text-sm mod-error-text"
                    )
            if chat_panel.subscribe_updates is not None:
                unsubscribe_updates = chat_panel.subscribe_updates(request_push_refresh)

                def _cleanup_chat_updates() -> None:
                    nonlocal page_closed
                    page_closed = True
                    unsubscribe_updates()

                self._register_client_cleanup(ui=ui, cleanup=_cleanup_chat_updates)
            else:
                refresh_timer = ui.timer(
                    _CHAT_HISTORY_REFRESH_INTERVAL_SECONDS,
                    lambda: asyncio.create_task(refresh_chat_messages()),
                )
                self._register_timer_cleanup(ui=ui, timer=refresh_timer)
        return apply_runtime_stats

    @staticmethod
    def _web_chat_session_id(*, app_name: str, user: ModWebUser, request: Request) -> str:
        request_identity = f"{id(request)}:{time.time_ns()}"
        digest = hashlib.sha256(
            f"{config.MOD_WEB_SERVER.node_name}:{app_name}:{user.discord_id}:{request_identity}".encode(
                config.STR_ENCODE
            )
        ).hexdigest()
        return digest[:24]

    @staticmethod
    def _ensure_chat_client_script(ui: ModWebUi) -> None:
        version = ModWebChatMixin._chat_client_asset_version()
        ui.add_head_html(f'<script src="/mod-web/assets/chat.js?v={version}"></script>')

    @staticmethod
    @lru_cache(maxsize=1)
    def _chat_client_javascript() -> str:
        return extract_html_tag_contents(ModWebChatMixin._chat_client_script(), tag_name="script")

    @staticmethod
    @lru_cache(maxsize=1)
    def _chat_client_asset_version() -> str:
        return hashlib.sha256(ModWebChatMixin._chat_client_javascript().encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _chat_history_append_count(previous_event_ids: tuple[str, ...], next_event_ids: tuple[str, ...]) -> int:
        max_overlap = min(len(previous_event_ids), len(next_event_ids))
        for overlap in range(max_overlap, 0, -1):
            if previous_event_ids[-overlap:] == next_event_ids[:overlap]:
                return len(next_event_ids) - overlap
        return len(next_event_ids)

    @staticmethod
    def _chat_client_script() -> str:
        return f"""
            <script>
            window.modWebChat = (() => {{
                const bindVersion = '2026-06-17-newest-first';
                const bottomThresholdPx = {_CHAT_TIMELINE_BOTTOM_THRESHOLD_PX};
                const autoScrollHiddenMessageLimit = 3;
                const get = (elementId) => document.getElementById(String(elementId)) || getElement(elementId);
                const disableLegacyScroll = () => {{
                  window.modWebChatTimelineObserver?.disconnect?.();
                  for (const timeline of document.querySelectorAll('.mod-chat-timeline')) {{
                    timeline.dataset.modChatSticky = '0';
                    timeline._modChatMutationObserver?.disconnect?.();
                    delete timeline._modChatMutationObserver;
                  }}
                }};
                const isScrollable = (element) => {{
                  if (!element) {{
                    return false;
                  }}
                  const style = window.getComputedStyle(element);
                  return element.scrollHeight > element.clientHeight + 1
                    || style.overflowY === 'auto'
                    || style.overflowY === 'scroll'
                    || style.overflowY === 'overlay';
                }};
                const scrollTargetFor = (timelineId) => {{
                  const root = get(timelineId);
                  if (!root) {{
                    return null;
                  }}
                  const candidates = [
                    root,
                    root.querySelector(':scope > .nicegui-content'),
                    ...root.querySelectorAll('.nicegui-content'),
                  ].filter(Boolean);
                  return candidates.find((candidate) => isScrollable(candidate)) || root;
                }};
                const jumpStateByTimeline = new WeakMap();
                const programmaticScrollTimelines = new WeakSet();
                const sticky = (_timeline) => false;
                const timelineAnchors = (timeline) =>
                  Array.from(timeline.querySelectorAll('.mod-chat-entry[data-mod-chat-event-id]'));
                const timelineEventIds = (timeline) =>
                  timelineAnchors(timeline)
                    .map((entry) => entry.dataset.modChatEventId || '')
                    .filter((eventId) => eventId.length > 0);
                const hiddenMessageCount = (previousEventIds, nextEventIds) => {{
                  const maxOverlap = Math.min(previousEventIds.length, nextEventIds.length);
                  for (let overlap = maxOverlap; overlap > 0; overlap -= 1) {{
                    const previousTail = previousEventIds.slice(previousEventIds.length - overlap);
                    const nextHead = nextEventIds.slice(0, overlap);
                    if (previousTail.join('\\u0000') === nextHead.join('\\u0000')) {{
                      return Math.max(0, nextEventIds.length - overlap);
                    }}
                  }}
                  return nextEventIds.length;
                }};
                const clampScrollTop = (timeline, value) =>
                  Math.max(0, Math.min(Math.max(0, timeline.scrollHeight - timeline.clientHeight), value));
                const setTimelineScrollTop = (timeline, value) => {{
                  programmaticScrollTimelines.add(timeline);
                  timeline.scrollTop = clampScrollTop(timeline, value);
                  requestAnimationFrame(() => programmaticScrollTimelines.delete(timeline));
                }};
                const setUnread = (timelineId, unreadBarId, unreadCountId, count) => {{
                  const timeline = scrollTargetFor(timelineId);
                  const unreadBar = get(unreadBarId);
                  const unreadCount = get(unreadCountId);
                  if (!timeline || !unreadBar || !unreadCount) {{
                    return;
                  }}
                  const nextCount = Math.max(0, count);
                  const wasVisible = unreadBar.style.display !== 'none';
                  timeline.dataset.modChatUnread = String(nextCount);
                  unreadCount.textContent = nextCount === 1 ? '1 new' : `${{nextCount}} new`;
                  unreadBar.style.display = nextCount > 0 ? 'flex' : 'none';
                  if (nextCount > 0 && !wasVisible) {{
                    unreadBar.classList.remove('mod-chat-unread-live');
                    void unreadBar.offsetWidth;
                    unreadBar.classList.add('mod-chat-unread-live');
                  }}
                }};
                const sync = (timelineId, unreadBarId, unreadCountId) => {{
                  const timeline = scrollTargetFor(timelineId);
                  if (!timeline) {{
                    return false;
                  }}
                  const remaining = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight;
                  const atBottom = remaining <= bottomThresholdPx;
                  if (atBottom) {{
                    setUnread(timelineId, unreadBarId, unreadCountId, 0);
                  }}
                  return atBottom;
                }};
                const relativeTime = (unixSeconds) => {{
                  const deltaSeconds = unixSeconds - Math.floor(Date.now() / 1000);
                  const absoluteDeltaSeconds = Math.abs(deltaSeconds);
                  let amount = absoluteDeltaSeconds;
                  let unit = 'second';
                  if (absoluteDeltaSeconds >= 86400) {{
                    amount = Math.floor(absoluteDeltaSeconds / 86400);
                    unit = 'day';
                  }} else if (absoluteDeltaSeconds >= 3600) {{
                    amount = Math.floor(absoluteDeltaSeconds / 3600);
                    unit = 'hour';
                  }} else if (absoluteDeltaSeconds >= 60) {{
                    amount = Math.floor(absoluteDeltaSeconds / 60);
                    unit = 'minute';
                  }}
                  const suffix = amount === 1 ? '' : 's';
                  if (deltaSeconds >= 0) {{
                    return `in ${{amount}} ${{unit}}${{suffix}}`;
                  }}
                  return `${{amount}} ${{unit}}${{suffix}} ago`;
                }};
                const defaultTimePreferences = Object.freeze({{use24HourTime: true}});
                const timePreferences = () => {{
                  const preferences = window.modWebPreferences;
                  if (!preferences || typeof preferences !== 'object') {{
                    return defaultTimePreferences;
                  }}
                  if (typeof preferences.use24HourTime !== 'boolean') {{
                    return defaultTimePreferences;
                  }}
                  return {{use24HourTime: preferences.use24HourTime}};
                }};
                const withTimePreferences = (options) => {{
                  const preferences = timePreferences();
                  if (!preferences.use24HourTime) {{
                    return options;
                  }}
                  return {{{{...options, hour12: false, hourCycle: 'h23'}}}};
                }};
                const timestampText = (unixSeconds, style) => {{
                  const date = new Date(unixSeconds * 1000);
                  if (Number.isNaN(date.getTime())) {{
                    return null;
                  }}
                  const resolvedStyle = style || 'f';
                  if (resolvedStyle === 'R') {{
                    return relativeTime(unixSeconds);
                  }}
                  if (resolvedStyle === 't') {{
                    return new Intl.DateTimeFormat(undefined, withTimePreferences({{timeStyle: 'short'}})).format(date);
                  }}
                  if (resolvedStyle === 'T') {{
                    return new Intl.DateTimeFormat(undefined, withTimePreferences({{timeStyle: 'medium'}})).format(date);
                  }}
                  if (resolvedStyle === 'd') {{
                    return new Intl.DateTimeFormat(undefined, withTimePreferences({{dateStyle: 'short'}})).format(date);
                  }}
                  if (resolvedStyle === 'D') {{
                    return new Intl.DateTimeFormat(undefined, withTimePreferences({{dateStyle: 'long'}})).format(date);
                  }}
                  if (resolvedStyle === 'F') {{
                    return new Intl.DateTimeFormat(
                      undefined,
                      withTimePreferences({{dateStyle: 'full', timeStyle: 'short'}}),
                    ).format(date);
                  }}
                  return new Intl.DateTimeFormat(
                    undefined,
                    withTimePreferences({{dateStyle: 'long', timeStyle: 'short'}}),
                  ).format(date);
                }};
                const timestampTitle = (unixSeconds) => {{
                  const date = new Date(unixSeconds * 1000);
                  if (Number.isNaN(date.getTime())) {{
                    return '';
                  }}
                  return new Intl.DateTimeFormat(
                    undefined,
                    withTimePreferences({{
                      dateStyle: 'full',
                      timeStyle: 'long',
                    }}),
                  ).format(date);
                }};
                const localizeTimes = (root) => {{
                  const scope = root || document;
                  for (const element of scope.querySelectorAll('.mod-chat-client-time[data-mod-chat-unix]')) {{
                    const unixSeconds = Number.parseInt(element.dataset.modChatUnix || '', 10);
                    if (!Number.isFinite(unixSeconds)) {{
                      continue;
                    }}
                    const style = element.dataset.modChatTimeStyle || 'f';
                    const text = timestampText(unixSeconds, style);
                    if (text !== null) {{
                      element.textContent = text;
                    }}
                    const title = timestampTitle(unixSeconds);
                    if (title) {{
                      element.title = title;
                    }}
                  }}
                }};
                const observeLocalizedTimes = () => {{
                  if (window.modWebChatTimeObserver || !document.body) {{
                    return;
                  }}
                  window.modWebChatTimeObserver = new MutationObserver((mutations) => {{
                    for (const mutation of mutations) {{
                      for (const node of mutation.addedNodes) {{
                        if (!(node instanceof Element)) {{
                          continue;
                        }}
                        if (node.matches('.mod-chat-client-time[data-mod-chat-unix]')) {{
                          localizeTimes(node.parentElement || node);
                          continue;
                        }}
                        if (node.querySelector('.mod-chat-client-time[data-mod-chat-unix]')) {{
                          localizeTimes(node);
                        }}
                      }}
                    }}
                  }});
                  window.modWebChatTimeObserver.observe(document.body, {{childList: true, subtree: true}});
                }};
                const attachMediaListeners = (timelineId, unreadBarId, unreadCountId) => {{
                  const timeline = scrollTargetFor(timelineId);
                  if (!timeline) {{
                    return;
                  }}
                  const onMediaReady = () => {{
                    if (sticky(timeline)) {{
                      jump(timelineId, unreadBarId, unreadCountId);
                    }} else {{
                      sync(timelineId, unreadBarId, unreadCountId);
                    }}
                  }};
                  for (const media of timeline.querySelectorAll('img, video, audio')) {{
                    if (media.dataset.modChatObserved === '1') {{
                      continue;
                    }}
                    media.dataset.modChatObserved = '1';
                    media.addEventListener('load', onMediaReady, {{passive: true}});
                    media.addEventListener('loadeddata', onMediaReady, {{passive: true}});
                    media.addEventListener('loadedmetadata', onMediaReady, {{passive: true}});
                    if (window.ResizeObserver && !media._modChatResizeObserver) {{
                      const resizeObserver = new ResizeObserver(() => onMediaReady());
                      resizeObserver.observe(media);
                      media._modChatResizeObserver = resizeObserver;
                    }}
                  }}
                }};
                const observeTimelineMutations = (timelineId, unreadBarId, unreadCountId) => {{
                  const timeline = scrollTargetFor(timelineId);
                  if (!timeline || timeline._modChatMutationObserver) {{
                    return;
                  }}
                  const observer = new MutationObserver((mutations) => {{
                    let structureChanged = false;
                    for (const mutation of mutations) {{
                      if (mutation.type !== 'childList') {{
                        continue;
                      }}
                      if (mutation.addedNodes.length > 0 || mutation.removedNodes.length > 0) {{
                        structureChanged = true;
                      }}
                      for (const node of mutation.addedNodes) {{
                        if (node instanceof Element) {{
                          localizeTimes(node);
                        }}
                      }}
                    }}
                    if (!structureChanged) {{
                      return;
                    }}
                    attachMediaListeners(timelineId, unreadBarId, unreadCountId);
                    if (sticky(timeline)) {{
                      jump(timelineId, unreadBarId, unreadCountId);
                    }} else {{
                      sync(timelineId, unreadBarId, unreadCountId);
                    }}
                  }});
                  observer.observe(timeline, {{childList: true, subtree: true}});
                  timeline._modChatMutationObserver = observer;
                }};
                const bind = (timelineId, unreadBarId, unreadCountId) => {{
                  const root = get(timelineId);
                  const timeline = scrollTargetFor(timelineId);
                  if (!timeline) {{
                    return;
                  }}
                  if (root?.dataset.modChatBound !== bindVersion) {{
                    root?._modChatMutationObserver?.disconnect?.();
                    if (root) {{
                      delete root._modChatMutationObserver;
                    }}
                    timeline._modChatMutationObserver?.disconnect?.();
                    delete timeline._modChatMutationObserver;
                    root.dataset.modChatBound = bindVersion;
                    timeline.dataset.modChatSticky = '0';
                    timeline.dataset.modChatUnread = timeline.dataset.modChatUnread || '0';
                    timeline.dataset.modChatWasPinned = timeline.dataset.modChatWasPinned || '0';
                    timeline.dataset.modChatHiddenCount = timeline.dataset.modChatHiddenCount || '0';
                    timeline.addEventListener('scroll', () => {{
                      if (!programmaticScrollTimelines.has(timeline)) {{
                        clearScheduledJump(timeline);
                        if (sticky(timeline)) {{
                          jump(timelineId, unreadBarId, unreadCountId);
                        }}
                      }}
                      sync(timelineId, unreadBarId, unreadCountId);
                    }}, {{passive: true}});
                    window.addEventListener('resize', () => {{
                      if (sticky(timeline)) {{
                        jump(timelineId, unreadBarId, unreadCountId);
                      }} else {{
                        sync(timelineId, unreadBarId, unreadCountId);
                      }}
                    }}, {{passive: true}});
                  }}
                  sync(timelineId, unreadBarId, unreadCountId);
                  attachMediaListeners(timelineId, unreadBarId, unreadCountId);
                  observeTimelineMutations(timelineId, unreadBarId, unreadCountId);
                  localizeTimes(timeline);
                }};
                const beforeRefresh = (timelineId, unreadBarId, unreadCountId) => {{
                  bind(timelineId, unreadBarId, unreadCountId);
                  const timeline = scrollTargetFor(timelineId);
                  if (!timeline) {{
                    return;
                  }}
                  timeline.dataset.modChatWasPinned = sticky(timeline) ? '1' : '0';
                  timeline.dataset.modChatPreviousEventIds = JSON.stringify(timelineEventIds(timeline));
                  timeline.dataset.modChatHiddenCount = '0';
                }};
                const clearScheduledJump = (timeline) => {{
                  const state = jumpStateByTimeline.get(timeline);
                  if (!state) {{
                    return;
                  }}
                  for (const frameId of state.frameIds) {{
                    cancelAnimationFrame(frameId);
                  }}
                  for (const timeoutId of state.timeoutIds) {{
                    clearTimeout(timeoutId);
                  }}
                  jumpStateByTimeline.delete(timeline);
                }};
                const scheduleTimelineTask = (timeline, settle) => {{
                  clearScheduledJump(timeline);
                  const frameIds = [];
                  const timeoutIds = [];
                  settle();
                  frameIds.push(requestAnimationFrame(settle));
                  frameIds.push(requestAnimationFrame(() => requestAnimationFrame(settle)));
                  timeoutIds.push(setTimeout(settle, 0));
                  timeoutIds.push(setTimeout(settle, 120));
                  timeoutIds.push(setTimeout(settle, 320));
                  timeoutIds.push(setTimeout(settle, 800));
                  timeoutIds.push(setTimeout(settle, 1500));
                  jumpStateByTimeline.set(timeline, {{frameIds, timeoutIds}});
                }};
                const settleBottom = (timelineId, unreadBarId, unreadCountId) => {{
                  const timeline = scrollTargetFor(timelineId);
                  if (!timeline) {{
                    return;
                  }}
                  const anchors = timelineAnchors(timeline);
                  const lastAnchor = anchors[anchors.length - 1];
                  lastAnchor?.scrollIntoView?.({{block: 'end', inline: 'nearest'}});
                  setTimelineScrollTop(timeline, timeline.scrollHeight);
                  setTimelineScrollTop(timeline, timeline.scrollHeight);
                  sync(timelineId, unreadBarId, unreadCountId);
                }};
                const jump = (timelineId, unreadBarId, unreadCountId) => {{
                  const timeline = scrollTargetFor(timelineId);
                  if (!timeline) {{
                    return;
                  }}
                  scheduleTimelineTask(timeline, () => settleBottom(timelineId, unreadBarId, unreadCountId));
                }};
                const shouldAutoScrollAfterRefresh = (timeline, forceScroll, appendedCount) => {{
                  if (forceScroll || sticky(timeline)) {{
                    return true;
                  }}
                  return timeline.dataset.modChatWasPinned === '1' && appendedCount <= autoScrollHiddenMessageLimit;
                }};
                const afterRefresh = (timelineId, unreadBarId, unreadCountId, appendedCount, forceScroll) => {{
                  bind(timelineId, unreadBarId, unreadCountId);
                  const timeline = scrollTargetFor(timelineId);
                  if (!timeline) {{
                    return;
                  }}
                  localizeTimes(timeline);
                  let previousEventIds = [];
                  try {{
                    previousEventIds = JSON.parse(timeline.dataset.modChatPreviousEventIds || '[]');
                  }} catch (_error) {{
                    previousEventIds = [];
                  }}
                  const previousEventIdSet = new Set(previousEventIds);
                  for (const entry of timeline.querySelectorAll('[data-mod-chat-event-id]')) {{
                    if (!previousEventIdSet.has(entry.dataset.modChatEventId)) {{
                      entry.classList.add('mod-chat-entry-live');
                    }}
                  }}
                  const hiddenCount = hiddenMessageCount(previousEventIds, timelineEventIds(timeline));
                  timeline.dataset.modChatHiddenCount = String(hiddenCount);
                  delete timeline.dataset.modChatPreviousEventIds;
                  const effectiveAppendCount = Math.max(0, Math.max(appendedCount, hiddenCount));
                  if (shouldAutoScrollAfterRefresh(timeline, forceScroll, effectiveAppendCount)) {{
                    jump(timelineId, unreadBarId, unreadCountId);
                    return;
                  }}
                  const currentUnread = Number.parseInt(timeline.dataset.modChatUnread || '0', 10) || 0;
                  setUnread(timelineId, unreadBarId, unreadCountId, currentUnread + effectiveAppendCount);
                }};
                const setSticky = (timelineId, unreadBarId, unreadCountId, stickyEnabled) => {{
                  bind(timelineId, unreadBarId, unreadCountId);
                  const timeline = scrollTargetFor(timelineId);
                  if (!timeline) {{
                    return;
                  }}
                  timeline.dataset.modChatSticky = stickyEnabled ? '1' : '0';
                  if (stickyEnabled) {{
                    setUnread(timelineId, unreadBarId, unreadCountId, 0);
                    jump(timelineId, unreadBarId, unreadCountId);
                  }} else {{
                    clearScheduledJump(timeline);
                    sync(timelineId, unreadBarId, unreadCountId);
                  }}
                }};
                const bindTimelineElement = (root, forceInitialJump) => {{
                  const shell = root.closest('.mod-chat-timeline-shell') || root.parentElement;
                  const unreadBar = shell?.querySelector?.('.mod-chat-unread-bar');
                  const unreadCount = unreadBar?.querySelector?.('.mod-chat-unread-count');
                  if (!root.id || !unreadBar?.id || !unreadCount?.id) {{
                    return;
                  }}
                  bind(root.id, unreadBar.id, unreadCount.id);
                  const timeline = scrollTargetFor(root.id);
                  if (
                    forceInitialJump
                    && timeline
                    && sticky(timeline)
                    && root.dataset.modChatInitialSettled !== bindVersion
                  ) {{
                    root.dataset.modChatInitialSettled = bindVersion;
                    jump(root.id, unreadBar.id, unreadCount.id);
                  }}
                }};
                const bindAllTimelines = (forceInitialJump = false) => {{
                  for (const root of document.querySelectorAll('.mod-chat-timeline')) {{
                    bindTimelineElement(root, forceInitialJump);
                  }}
                }};
                const observeTimelines = () => {{
                  window.modWebChatTimelineObserver?.disconnect?.();
                  if (!document.body) {{
                    return;
                  }}
                  let queued = false;
                  const flush = () => {{
                    queued = false;
                    bindAllTimelines(true);
                  }};
                  const queue = () => {{
                    if (queued) {{
                      return;
                    }}
                    queued = true;
                    requestAnimationFrame(flush);
                  }};
                  window.modWebChatTimelineObserver = new MutationObserver((mutations) => {{
                    for (const mutation of mutations) {{
                      for (const node of mutation.addedNodes) {{
                        if (!(node instanceof Element)) {{
                          continue;
                        }}
                        if (node.matches('.mod-chat-timeline') || node.querySelector('.mod-chat-timeline')) {{
                          queue();
                          return;
                        }}
                      }}
                    }}
                  }});
                  window.modWebChatTimelineObserver.observe(document.body, {{childList: true, subtree: true}});
                  bindAllTimelines(true);
                }};
                disableLegacyScroll();
                localizeTimes(document);
                observeLocalizedTimes();
                if (window.modWebChatTimeInterval) {{
                  window.clearInterval(window.modWebChatTimeInterval);
                }}
                window.modWebChatTimeInterval = window.setInterval(() => localizeTimes(document), 30000);
                return {{localizeTimes, disableLegacyScroll}};
            }})();
            </script>
            """

    @staticmethod
    def _chat_history_signature(events: tuple[ChatEvent, ...]) -> tuple[str, ...]:
        return tuple(event.id for event in events)

    def _chat_event_reference(self, event: ChatEvent) -> ChatMessageReference:
        app = self._chat_room_app(event.room_id)
        app_friendly = getattr(app, "friendly", event.room_id) if app is not None else event.room_id
        return event.to_reference(app_name=app_friendly)

    @classmethod
    def _chat_event_groups(cls, events: tuple[ChatEvent, ...]) -> tuple[_ModWebChatEventGroup, ...]:
        groups: list[_ModWebChatEventGroup] = []
        active_events: list[ChatEvent] = []
        active_head: ChatEvent | None = None
        previous_event: ChatEvent | None = None
        for event in events:
            if previous_event is None or not cls._can_group_chat_events(previous_event, event):
                if active_head is not None and active_events:
                    groups.append(_ModWebChatEventGroup(head_event=active_head, events=tuple(active_events)))
                active_head = event
                active_events = [event]
            else:
                active_events.append(event)
            previous_event = event
        if active_head is not None and active_events:
            groups.append(_ModWebChatEventGroup(head_event=active_head, events=tuple(active_events)))
        return tuple(groups)

    @classmethod
    def _can_group_chat_events(cls, previous: ChatEvent, current: ChatEvent) -> bool:
        if not cls._is_groupable_chat_event(previous) or not cls._is_groupable_chat_event(current):
            return False
        if cls._chat_event_author_key(previous) != cls._chat_event_author_key(current):
            return False
        if previous.source.stable_key != current.source.stable_key:
            return False
        return current.created_at - previous.created_at <= _CHAT_GROUP_WINDOW_SECONDS

    @classmethod
    def _is_groupable_chat_event(cls, event: ChatEvent) -> bool:
        if event.author.kind is ChatAuthorKind.SYSTEM:
            return False
        if event.notice is not None or event.embed is not None:
            return False
        return not cls._chat_event_badges(event)

    @staticmethod
    def _chat_event_author_key(event: ChatEvent) -> str:
        if event.author.discord_user_id is not None:
            return f"discord:{event.author.discord_user_id}"
        if event.author.id is not None:
            return f"id:{event.author.id}"
        return f"name:{event.author.display_name.casefold()}"

    def _render_chat_event_group(
        self,
        *,
        ui: ModWebUi,
        group: _ModWebChatEventGroup,
        room_id: str,
        can_reply: bool,
        on_reply: Callable[[ChatEvent], None],
    ) -> Element:
        head_event = group.head_event
        event_badges = self._chat_event_badges(head_event)
        with ui.element("article").classes(
            f"mod-chat-message {self._chat_event_source_class(head_event)} w-full"
        ) as group_root:
            with ui.column().classes("mod-chat-message-inner w-full"):
                with ui.row().classes("mod-chat-message-head w-full items-center justify-between gap-2 flex-wrap"):
                    with ui.row().classes("mod-chat-author-row items-center gap-2 min-w-0"):
                        self._render_chat_author_avatar(ui=ui, event=head_event)
                        author_label = ui.label(self._chat_event_author_display_name(head_event)).classes(
                            "mod-chat-author break-all"
                        )
                        author_label.style(f"color: {self._chat_author_color_hex(head_event)} !important;")
                    with ui.row().classes("mod-chat-head-meta items-center gap-2 flex-wrap justify-end ml-auto"):
                        with ui.row().classes("mod-chat-badge-row items-center gap-1 flex-wrap justify-end"):
                            for badge in event_badges:
                                self._badge(
                                    ui=ui,
                                    text=badge.text,
                                    tone=badge.tone,
                                    extra_classes="mod-chat-source-badge",
                                )
                            self._badge(
                                ui=ui,
                                text=self._chat_event_source_label(head_event, room_id=room_id),
                                tone=self._chat_event_tone(head_event),
                                extra_classes="mod-chat-source-badge",
                            )
                with ui.column().classes("mod-chat-entry-list w-full"):
                    for event in group.events:
                        self._render_chat_event_entry(
                            ui=ui,
                            event=event,
                            show_time=True,
                            can_reply=can_reply,
                            on_reply=on_reply,
                        )
        return group_root

    def _render_chat_event_entry(
        self,
        *,
        ui: ModWebUi,
        event: ChatEvent,
        show_time: bool,
        can_reply: bool,
        on_reply: Callable[[ChatEvent], None],
    ) -> None:
        copy_text: str = self._chat_event_copy_text(event)
        with (
            ui.row()
            .classes("mod-chat-entry w-full items-start gap-2")
            .props(f'data-mod-chat-event-id="{escape(event.id)}"')
        ):
            with ui.column().classes("mod-chat-entry-main min-w-0 grow"):
                self._render_chat_event_body(ui=ui, event=event)
            with ui.row().classes("mod-chat-entry-meta items-start justify-end gap-1"):
                if show_time:
                    ui.html(self._chat_event_time_markup(event)).classes("mod-chat-entry-time")
            if can_reply or copy_text:
                with ui.context_menu().classes("mod-chat-entry-menu"):
                    if copy_text:
                        ui.menu_item(
                            "Copy",
                            on_click=lambda ui=ui, copy_text=copy_text: copy_text_to_clipboard(
                                ui=ui,
                                text=copy_text,
                                empty_message="This message has no text to copy.",
                            ),
                        ).classes("mod-chat-entry-menu-item")
                    if can_reply:
                        ui.menu_item("Reply", on_click=lambda event=event: on_reply(event)).classes(
                            "mod-chat-entry-menu-item"
                        )

    def _render_chat_event_body(self, *, ui: ModWebUi, event: ChatEvent) -> None:
        reference = event.reference
        if reference is not None:
            with ui.column().classes("mod-chat-reference w-full"):
                ui.label(
                    self._chat_reference_label(
                        event.reference_kind,
                        reference,
                        room_id=event.room_id,
                        preferred_guild_id=event.source_guild_id,
                    )
                ).classes("mod-chat-reference-label")
                self._render_chat_markup(
                    ui=ui,
                    text=reference.content,
                    classes="mod-chat-reference-content mod-chat-markup break-words",
                    room_id=event.room_id,
                    preferred_guild_id=event.source_guild_id,
                )
        rendered_content = self._chat_event_display_content(event)
        if rendered_content:
            self._render_chat_markup(
                ui=ui,
                text=rendered_content,
                classes="mod-chat-content mod-chat-markup break-words",
                room_id=event.room_id,
                preferred_guild_id=event.source_guild_id,
            )
        link_previews = tuple(self._chat_media_preview_from_link(link) for link in event.links)
        attachment_previews = tuple(
            self._chat_media_preview_from_attachment(attachment) for attachment in event.attachments
        )
        media_previews = tuple(preview for preview in (*link_previews, *attachment_previews) if preview is not None)
        if media_previews:
            with ui.row().classes("mod-chat-media-grid w-full"):
                for preview in media_previews:
                    ui.html(self._chat_media_embed_markup(preview)).classes("mod-chat-media-card")
        if event.links:
            with ui.row().classes("mod-chat-asset-row w-full"):
                for link, preview in zip(event.links, link_previews, strict=True):
                    if preview is not None:
                        continue
                    self._external_chat_link(
                        ui=ui,
                        label=link.label or link.url,
                        url=link.url,
                    ).classes("mod-row-download mod-chat-asset")
        if event.attachments:
            with ui.row().classes("mod-chat-asset-row w-full"):
                for attachment, preview in zip(event.attachments, attachment_previews, strict=True):
                    if preview is not None:
                        continue
                    attachment_url = self._chat_attachment_url(
                        attachment_uri=attachment.uri, source_url=attachment.source_url
                    )
                    if attachment_url is None:
                        self._badge(ui=ui, text=attachment.name, tone="grey")
                    else:
                        self._external_chat_link(
                            ui=ui,
                            label=attachment.name,
                            url=attachment_url,
                        ).classes("mod-row-download mod-chat-asset")

    def _chat_reference_label(
        self,
        reference_kind: ChatReferenceKind,
        reference: ChatMessageReference,
        *,
        room_id: str,
        preferred_guild_id: int | None,
    ) -> str:
        author_display_name = self._chat_reference_author_display_name(
            reference,
            room_id=room_id,
            preferred_guild_id=preferred_guild_id,
        )
        if reference_kind is ChatReferenceKind.FORWARD:
            return f"Forwarded from {author_display_name}"
        return f"Replying to {author_display_name}"

    def _render_chat_markup(
        self,
        *,
        ui: ModWebUi,
        text: str,
        classes: str,
        room_id: str,
        preferred_guild_id: int | None = None,
    ) -> None:
        markup = self._chat_markup_html(
            text,
            text_transform=lambda plain_text: self._resolve_chat_markup_mentions(
                plain_text,
                room_id=room_id,
                preferred_guild_id=preferred_guild_id,
            ),
        )
        if not markup:
            return
        ui.html(markup).classes(classes)

    @classmethod
    def _chat_markup_html(
        cls,
        text: str,
        *,
        text_transform: Callable[[str], str] | None = None,
    ) -> str:
        if not text:
            return ""
        text_with_placeholders, placeholders = cls._chat_markup_preserve_code(text)
        text_with_placeholders, placeholders = cls._chat_markup_preserve_escapes(text_with_placeholders, placeholders)
        if text_transform is not None:
            text_with_placeholders = text_transform(text_with_placeholders)
        text_with_placeholders, placeholders = cls._chat_markup_preserve_links(
            text_with_placeholders,
            placeholders,
        )
        text_with_placeholders, placeholders = cls._chat_markup_preserve_discord_timestamps(
            text_with_placeholders,
            placeholders,
        )
        text_with_placeholders, placeholders = cls._chat_markup_preserve_raw_urls(
            text_with_placeholders,
            placeholders,
        )
        segments = cls._chat_markup_segments(text_with_placeholders, placeholders)
        return "".join(segments)

    def _resolve_chat_markup_mentions(
        self,
        text: str,
        *,
        room_id: str,
        preferred_guild_id: int | None,
    ) -> str:
        return self._resolve_chat_mentions(
            text,
            room_id=room_id,
            preferred_guild_id=preferred_guild_id,
            prefix="@",
        )

    def _resolve_chat_display_mentions(
        self,
        text: str,
        *,
        room_id: str,
        preferred_guild_id: int | None,
    ) -> str:
        return self._resolve_chat_mentions(
            text,
            room_id=room_id,
            preferred_guild_id=preferred_guild_id,
            prefix="",
        )

    def _resolve_chat_mentions(
        self,
        text: str,
        *,
        room_id: str,
        preferred_guild_id: int | None,
        prefix: str,
    ) -> str:
        if not text:
            return text
        scope = self._chat_room_scope(room_id)
        platforms = self._chat_room_platforms(room_id)
        preferred_platform = self._chat_room_preferred_platform(room_id)
        name_cache = config.Name_Cache()

        def replace_user_mention(match: re.Match[str]) -> str:
            raw_user_id = match.group("discord_user_id") or match.group("raw_discord_user_id")
            if raw_user_id is None:
                raise ValueError("Chat mention match is missing a Discord user ID.")
            user_id = int(raw_user_id)
            display_name = name_cache.web_mention_name(
                user_id,
                scope=scope,
                platforms=platforms,
                preferred_platform=preferred_platform,
                default=str(user_id),
            )
            return f"{prefix}{display_name}"

        def replace_channel_mention(match: re.Match[str]) -> str:
            channel_id = int(match.group("channel_id"))
            channel_name = self._discord_chat_channel_name_by_id(channel_id)
            return f"#{channel_name or channel_id}"

        def replace_role_mention(match: re.Match[str]) -> str:
            role_id = int(match.group("role_id"))
            role_name = self._discord_role_name_by_id(role_id)
            return f"@{role_name or role_id}"

        def replace_timestamp(match: re.Match[str]) -> str:
            return match.group(0)

        resolved = _CHAT_MARKUP_DISCORD_MENTION_RE.sub(replace_user_mention, text)
        resolved = _CHAT_MARKUP_DISCORD_CHANNEL_RE.sub(replace_channel_mention, resolved)
        resolved = _CHAT_MARKUP_DISCORD_ROLE_RE.sub(replace_role_mention, resolved)
        return _CHAT_MARKUP_DISCORD_TIMESTAMP_RE.sub(replace_timestamp, resolved)

    def _chat_event_author_display_name(self, event: ChatEvent) -> str:
        return self._chat_identity_display_name(
            display_name=event.author.display_name,
            room_id=event.room_id,
            preferred_guild_id=event.source_guild_id,
            discord_user_id=event.author.discord_user_id,
        )

    def _chat_reference_author_display_name(
        self,
        reference: ChatMessageReference,
        *,
        room_id: str,
        preferred_guild_id: int | None,
    ) -> str:
        discord_user_id = reference.discord_user_id
        if discord_user_id is None:
            match = _CHAT_MARKUP_DISCORD_MENTION_RE.fullmatch(reference.author_display_name.strip())
            if match is not None:
                raw_user_id = match.group("discord_user_id") or match.group("raw_discord_user_id")
                if raw_user_id is not None:
                    discord_user_id = int(raw_user_id)
        return self._chat_identity_display_name(
            display_name=reference.author_display_name,
            room_id=room_id,
            preferred_guild_id=preferred_guild_id,
            discord_user_id=discord_user_id,
        )

    def _chat_identity_display_name(
        self,
        *,
        display_name: str,
        room_id: str,
        preferred_guild_id: int | None,
        discord_user_id: int | None = None,
    ) -> str:
        if not display_name:
            return display_name
        if discord_user_id is not None:
            scope = self._chat_room_scope(room_id)
            platforms = self._chat_room_platforms(room_id)
            preferred_platform = self._chat_room_preferred_platform(room_id)
            return config.Name_Cache().web_display_name(
                discord_user_id,
                display_name if not self._is_raw_chat_discord_mention(display_name, discord_user_id) else str(discord_user_id),
                scope=scope,
                platforms=platforms,
                preferred_platform=preferred_platform,
            )
        return self._resolve_chat_display_mentions(
            display_name,
            room_id=room_id,
            preferred_guild_id=preferred_guild_id,
        )

    @staticmethod
    def _is_raw_chat_discord_mention(text: str, discord_user_id: int) -> bool:
        match = _CHAT_MARKUP_DISCORD_MENTION_RE.fullmatch(text.strip())
        if match is None:
            return False
        raw_user_id = match.group("discord_user_id") or match.group("raw_discord_user_id")
        if raw_user_id is None:
            return False
        return int(raw_user_id) == discord_user_id

    @classmethod
    def _chat_markup_preserve_code(cls, text: str) -> tuple[str, dict[str, str]]:
        placeholders: dict[str, str] = {}

        def replace_code_block(match: re.Match[str]) -> str:
            code_text = match.group(1).strip("\n")
            return cls._chat_markup_store_fragment(
                placeholders,
                f'<pre class="mod-chat-code-block"><code>{escape(code_text)}</code></pre>',
            )

        def replace_inline_code(match: re.Match[str]) -> str:
            return cls._chat_markup_store_fragment(
                placeholders,
                f'<code class="mod-chat-inline-code">{escape(match.group(1))}</code>',
            )

        with_code_blocks = _CHAT_MARKUP_CODE_BLOCK_RE.sub(replace_code_block, text)
        with_inline_code = _CHAT_MARKUP_INLINE_CODE_RE.sub(replace_inline_code, with_code_blocks)
        return with_inline_code, placeholders

    @classmethod
    def _chat_markup_preserve_escapes(
        cls,
        text: str,
        placeholders: dict[str, str],
    ) -> tuple[str, dict[str, str]]:
        if not text:
            return text, placeholders

        def replace_escape(match: re.Match[str]) -> str:
            return cls._chat_markup_store_fragment(placeholders, escape(match.group("escaped")))

        return _CHAT_MARKUP_ESCAPE_RE.sub(replace_escape, text), placeholders

    @staticmethod
    def _chat_markup_store_fragment(placeholders: dict[str, str], fragment_html: str) -> str:
        placeholder = f"MODWEBCHATPLACEHOLDER{len(placeholders)}TOKEN"
        placeholders[placeholder] = fragment_html
        return placeholder

    @classmethod
    def _chat_markup_preserve_links(
        cls,
        text: str,
        placeholders: dict[str, str],
    ) -> tuple[str, dict[str, str]]:
        if not text:
            return text, placeholders

        def replace_link(match: re.Match[str]) -> str:
            url = match.group("url")
            if not cls._is_safe_chat_media_url(url):
                return escape(match.group(0))
            safe_url = escape(url, quote=True)
            safe_label = escape(match.group("label"))
            return cls._chat_markup_store_fragment(
                placeholders,
                f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">{safe_label}</a>'
            )

        return _CHAT_MARKUP_LINK_RE.sub(replace_link, text), placeholders

    @classmethod
    def _chat_markup_preserve_raw_urls(
        cls,
        text: str,
        placeholders: dict[str, str],
    ) -> tuple[str, dict[str, str]]:
        if not text:
            return text, placeholders

        def replace_url(match: re.Match[str]) -> str:
            matched_url = match.group("url")
            url, trailing_punctuation = cls._split_chat_trailing_url_punctuation(matched_url)
            if not url or not cls._is_safe_chat_media_url(url):
                return escape(matched_url)
            safe_url = escape(url, quote=True)
            safe_label = escape(url)
            anchor_html = (
                f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">{safe_label}</a>'
            )
            trailing_html = escape(trailing_punctuation)
            return cls._chat_markup_store_fragment(placeholders, f"{anchor_html}{trailing_html}")

        return _CHAT_MARKUP_RAW_URL_RE.sub(replace_url, text), placeholders

    @classmethod
    def _chat_markup_preserve_discord_timestamps(
        cls,
        text: str,
        placeholders: dict[str, str],
    ) -> tuple[str, dict[str, str]]:
        if not text:
            return text, placeholders

        def replace_timestamp(match: re.Match[str]) -> str:
            unix_timestamp = int(match.group("unix"))
            style = match.group("style") or "f"
            return cls._chat_markup_store_fragment(
                placeholders,
                cls._client_local_time_markup(unix_timestamp=unix_timestamp, style=style),
            )

        return _CHAT_MARKUP_DISCORD_TIMESTAMP_RE.sub(replace_timestamp, text), placeholders

    @staticmethod
    def _split_chat_trailing_url_punctuation(url: str) -> tuple[str, str]:
        trimmed_url = url
        trailing_characters: list[str] = []
        while trimmed_url and trimmed_url[-1] in ".,!?;:":
            trailing_characters.append(trimmed_url[-1])
            trimmed_url = trimmed_url[:-1]
        while trimmed_url.endswith(")") and trimmed_url.count(")") > trimmed_url.count("("):
            trailing_characters.append(")")
            trimmed_url = trimmed_url[:-1]
        return trimmed_url, "".join(reversed(trailing_characters))

    @classmethod
    def _client_local_time_markup(cls, *, unix_timestamp: int, style: str) -> str:
        safe_style = escape(style, quote=True)
        fallback_text = escape(cls._discord_timestamp_fallback_text(unix_timestamp=unix_timestamp, style=style))
        iso_datetime = escape(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(unix_timestamp)), quote=True)
        return (
            '<time class="mod-chat-client-time"'
            f' datetime="{iso_datetime}"'
            f' data-mod-chat-unix="{unix_timestamp}"'
            f' data-mod-chat-time-style="{safe_style}">{fallback_text}</time>'
        )

    @classmethod
    def _discord_timestamp_fallback_text(cls, *, unix_timestamp: int, style: str) -> str:
        if style == "R":
            return cls._discord_relative_timestamp_text(unix_timestamp=unix_timestamp)
        timestamp = time.gmtime(unix_timestamp)
        if style == "t":
            return time.strftime("%H:%M UTC", timestamp)
        if style == "T":
            return time.strftime("%H:%M:%S UTC", timestamp)
        if style == "d":
            return time.strftime("%Y-%m-%d UTC", timestamp)
        if style == "D":
            return time.strftime("%B %d, %Y UTC", timestamp)
        if style == "F":
            return time.strftime("%A, %B %d, %Y %H:%M UTC", timestamp)
        return time.strftime("%B %d, %Y %H:%M UTC", timestamp)

    @classmethod
    def _chat_markup_segments(cls, text: str, placeholders: Mapping[str, str]) -> tuple[str, ...]:
        lines: list[str] = text.splitlines()
        if not lines:
            inline_html: str = cls._chat_markup_inline_html(text, placeholders)
            if not inline_html:
                return ()
            return (f'<div class="mod-chat-markup-block">{inline_html}</div>',)

        segments: list[str] = []
        normal_lines: list[str] = []
        index = 0
        line_count: int = len(lines)

        def flush_normal_lines() -> None:
            if not normal_lines:
                return
            rendered_lines: list[str] = [cls._chat_markup_inline_html(line, placeholders) for line in normal_lines]
            segments.append(f'<div class="mod-chat-markup-block">{"<br>".join(rendered_lines)}</div>')
            normal_lines.clear()

        while index < line_count:
            line: str = lines[index]
            header_match = _CHAT_MARKUP_HEADER_RE.fullmatch(line)
            if header_match is not None:
                flush_normal_lines()
                content = cls._chat_markup_inline_html(header_match.group("content"), placeholders)
                level = len(header_match.group("level"))
                segments.append(
                    f'<div class="mod-chat-markup-heading mod-chat-markup-heading-{level}">{content}</div>'
                )
                index += 1
                continue
            subtext_match = _CHAT_MARKUP_SUBTEXT_RE.fullmatch(line)
            if subtext_match is not None:
                flush_normal_lines()
                content = cls._chat_markup_inline_html(subtext_match.group("content"), placeholders)
                segments.append(f'<div class="mod-chat-markup-subtext">{content}</div>')
                index += 1
                continue
            if cls._chat_markup_list_item_info(line) is not None:
                flush_normal_lines()
                list_markup, index = cls._chat_markup_list_html(lines, index, placeholders)
                segments.append(list_markup)
                continue
            if line.startswith(">>>"):
                flush_normal_lines()
                quote_lines = [cls._chat_markup_strip_quote_prefix(line, prefix=">>>")]
                quote_lines.extend(lines[index + 1 :])
                rendered_quote = "<br>".join(
                    cls._chat_markup_inline_html(quote_line, placeholders) for quote_line in quote_lines
                )
                segments.append(f'<blockquote class="mod-chat-quote">{rendered_quote}</blockquote>')
                break
            if line.startswith(">") and not line.startswith(">>>"):
                flush_normal_lines()
                quote_lines: list[str] = []
                while index < line_count and lines[index].startswith(">") and not lines[index].startswith(">>>"):
                    quote_lines.append(cls._chat_markup_strip_quote_prefix(lines[index], prefix=">"))
                    index += 1
                rendered_quote: str = "<br>".join(
                    cls._chat_markup_inline_html(quote_line, placeholders) for quote_line in quote_lines
                )
                segments.append(f'<blockquote class="mod-chat-quote">{rendered_quote}</blockquote>')
                continue
            normal_lines.append(line)
            index += 1
        flush_normal_lines()
        return tuple[str, ...](segments)

    @classmethod
    def _chat_markup_list_html(
        cls,
        lines: list[str],
        start_index: int,
        placeholders: Mapping[str, str],
    ) -> tuple[str, int]:
        first_item = cls._chat_markup_list_item_info(lines[start_index])
        if first_item is None:
            raise ValueError("List markup rendering requires a list item at the starting index.")
        return cls._chat_markup_render_list_block(
            lines,
            start_index,
            placeholders,
            expected_depth=first_item[0],
        )

    @classmethod
    def _chat_markup_render_list_block(
        cls,
        lines: list[str],
        start_index: int,
        placeholders: Mapping[str, str],
        *,
        expected_depth: int,
    ) -> tuple[str, int]:
        first_item = cls._chat_markup_list_item_info(lines[start_index])
        if first_item is None or first_item[0] != expected_depth:
            raise ValueError("List block rendering requires a matching starting depth.")
        ordered = first_item[1]
        start_number = first_item[2]
        list_tag = "ol" if ordered else "ul"
        list_classes = (
            "mod-chat-markup-list mod-chat-markup-list-ordered"
            if ordered
            else "mod-chat-markup-list mod-chat-markup-list-unordered"
        )
        start_attribute = ""
        if ordered and start_number is not None and start_number != 1:
            start_attribute = f' start="{start_number}"'
        html_parts: list[str] = [f"<{list_tag} class=\"{list_classes}\"{start_attribute}>"]
        index = start_index
        opened_item = False
        while index < len(lines):
            item = cls._chat_markup_list_item_info(lines[index])
            if item is None or item[0] < expected_depth:
                break
            if item[0] > expected_depth:
                if item[0] != expected_depth + 1 or not opened_item:
                    break
                nested_html, index = cls._chat_markup_render_list_block(
                    lines,
                    index,
                    placeholders,
                    expected_depth=item[0],
                )
                html_parts.append(nested_html)
                continue
            if item[1] != ordered:
                break
            if opened_item:
                html_parts.append("</li>")
            html_parts.append("<li>")
            html_parts.append(cls._chat_markup_inline_html(item[3], placeholders))
            opened_item = True
            index += 1
        if opened_item:
            html_parts.append("</li>")
        html_parts.append(f"</{list_tag}>")
        return "".join(html_parts), index

    @staticmethod
    def _chat_markup_list_item_info(line: str) -> tuple[int, bool, int | None, str] | None:
        ordered_match = _CHAT_MARKUP_ORDERED_LIST_RE.fullmatch(line)
        if ordered_match is not None:
            indent = ordered_match.group("indent")
            if len(indent) % 2 != 0:
                return None
            return (
                len(indent) // 2,
                True,
                int(ordered_match.group("number")),
                ordered_match.group("content"),
            )
        unordered_match = _CHAT_MARKUP_UNORDERED_LIST_RE.fullmatch(line)
        if unordered_match is None:
            return None
        indent = unordered_match.group("indent")
        if len(indent) % 2 != 0:
            return None
        return (
            len(indent) // 2,
            False,
            None,
            unordered_match.group("content"),
        )

    @staticmethod
    def _chat_markup_strip_quote_prefix(line: str, *, prefix: str) -> str:
        stripped_line: str = line[len(prefix) :]
        if stripped_line.startswith(" "):
            return stripped_line[1:]
        return stripped_line

    @classmethod
    def _chat_markup_inline_html(cls, text: str, placeholders: Mapping[str, str]) -> str:
        rendered_text = escape(text)
        rendered_text = _CHAT_MARKUP_UNDERLINE_RE.sub(r"<u>\1</u>", rendered_text)
        rendered_text = _CHAT_MARKUP_BOLD_RE.sub(r"<strong>\1</strong>", rendered_text)
        rendered_text = _CHAT_MARKUP_STRIKETHROUGH_RE.sub(r"<s>\1</s>", rendered_text)
        rendered_text = _CHAT_MARKUP_ITALIC_STAR_RE.sub(r"<em>\1</em>", rendered_text)
        rendered_text = _CHAT_MARKUP_ITALIC_UNDERSCORE_RE.sub(r"<em>\1</em>", rendered_text)
        rendered_text = _CHAT_MARKUP_SPOILER_RE.sub(
            r'<span class="mod-chat-spoiler" tabindex="0">\1</span>',
            rendered_text,
        )
        for placeholder, fragment_html in placeholders.items():
            rendered_text: str = rendered_text.replace(placeholder, fragment_html)
        return rendered_text

    @staticmethod
    def _chat_attachment_url(*, attachment_uri: str, source_url: str | None) -> str | None:
        if source_url is not None and source_url.startswith(("http://", "https://")):
            return source_url
        if attachment_uri.startswith(("http://", "https://")):
            return attachment_uri
        return None

    @staticmethod
    def _external_chat_link(*, ui: ModWebUi, label: str, url: str) -> "Link":
        return ui.link(label, url).props('target="_blank" rel="noopener noreferrer"')

    @classmethod
    def _chat_media_preview_from_link(cls, link: ChatLink) -> _ChatMediaPreview | None:
        if not cls._is_safe_chat_media_url(link.url):
            return None
        kind: ChatMediaPreviewKind | None = cls._chat_media_preview_kind(
            url=link.url,
            label=link.label,
            media_type=link.media_type,
            extension=link.extension,
        )
        if kind is None:
            return None
        return _ChatMediaPreview(kind=kind, url=link.url, label=link.label or link.url)

    @classmethod
    def _chat_media_preview_from_attachment(cls, attachment: ChatAttachment) -> _ChatMediaPreview | None:
        url: str | None = cls._chat_attachment_url(attachment_uri=attachment.uri, source_url=attachment.source_url)
        if url is None or not cls._is_safe_chat_media_url(url):
            return None
        kind: ChatMediaPreviewKind | None = cls._chat_media_preview_kind(
            url=url,
            label=attachment.name,
            media_type=None,
            extension=PurePosixPath(attachment.name).suffix,
        )
        if kind is None:
            return None
        return _ChatMediaPreview(kind=kind, url=url, label=attachment.name)

    @staticmethod
    def _is_safe_chat_media_url(url: str) -> bool:
        return url.startswith(("http://", "https://"))

    @staticmethod
    def _chat_media_preview_kind(
        *,
        url: str,
        label: str | None,
        media_type: str | None,
        extension: str | None,
    ) -> ChatMediaPreviewKind | None:
        guessed_type = media_type or mimetypes.guess_type(urlsplit(url).path)[0]
        if guessed_type is None and label is not None:
            guessed_type: str | None = mimetypes.guess_type(label)[0]
        if guessed_type is not None:
            normalised_type: str = guessed_type.casefold()
            if normalised_type.startswith("image/"):
                return "image"
            if normalised_type.startswith("video/"):
                return "video"
            if normalised_type.startswith("audio/"):
                return "audio"

        extensions: tuple[str | None, str, str | None] = (
            extension,
            PurePosixPath(urlsplit(url).path).suffix,
            PurePosixPath(label).suffix if label is not None else None,
        )
        normalised_extensions: set[str] = {candidate.casefold() for candidate in extensions if candidate}
        if normalised_extensions & _CHAT_MEDIA_IMAGE_EXTENSIONS:
            return "image"
        if normalised_extensions & _CHAT_MEDIA_VIDEO_EXTENSIONS:
            return "video"
        if normalised_extensions & _CHAT_MEDIA_AUDIO_EXTENSIONS:
            return "audio"
        return None

    @staticmethod
    def _chat_media_embed_markup(preview: _ChatMediaPreview) -> str:
        safe_url: str = escape(preview.url, quote=True)
        safe_label: str = escape(preview.label, quote=True)
        if preview.kind == "image":
            media = f'<img class="mod-chat-media-image" src="{safe_url}" alt="{safe_label}" loading="lazy">'
        elif preview.kind == "video":
            media = f'<video class="mod-chat-media-video" src="{safe_url}" controls preload="metadata"></video>'
        elif preview.kind == "audio":
            media: str = f'<audio class="mod-chat-media-audio" src="{safe_url}" controls preload="metadata"></audio>'
        else:
            raise ValueError(f"Unsupported chat media preview kind: {preview.kind}")
        return (
            f'<div class="mod-chat-media-link">{media}'
            f'<a class="mod-chat-media-caption" href="{safe_url}" target="_blank" rel="noopener noreferrer">'
            f"{safe_label}</a></div>"
        )

    @classmethod
    def _chat_event_time_markup(cls, event: ChatEvent) -> str:
        unix_timestamp = int(event.created_at)
        return cls._client_local_time_markup(unix_timestamp=unix_timestamp, style="T")

    @staticmethod
    def _chat_endpoint_count_text(snapshot: NodeChatRoomSnapshot) -> str:
        return f"{snapshot.endpoint_count} endpoints"

    @staticmethod
    def _chat_endpoint_count_tooltip(snapshot: NodeChatRoomSnapshot) -> str | None:
        if not snapshot.endpoint_summaries:
            return None
        return "<br>".join(escape(summary.label) for summary in snapshot.endpoint_summaries)

    @classmethod
    def _render_chat_endpoint_badge(
        cls,
        *,
        ui: ModWebUi,
        snapshot: NodeChatRoomSnapshot,
    ) -> tuple["Label", "Tooltip", Html]:
        badge: Label = ModWebUiHelpersMixin._badge(ui=ui, text=cls._chat_endpoint_count_text(snapshot), tone="black")
        tooltip, tooltip_content = ModWebUiHelpersMixin._attach_html_tooltip(
            ui=ui,
            target=badge,
            html=cls._chat_endpoint_count_tooltip(snapshot) or "",
        )
        return badge, tooltip, tooltip_content

    @classmethod
    def _set_chat_endpoint_badge_state(
        cls,
        badge: "Label",
        tooltip: "Tooltip",
        tooltip_content: Html,
        snapshot: NodeChatRoomSnapshot,
    ) -> None:
        badge.set_text(cls._chat_endpoint_count_text(snapshot))
        ModWebUiHelpersMixin._set_html_tooltip_state(
            tooltip,
            tooltip_content,
            cls._chat_endpoint_count_tooltip(snapshot) or "",
        )
        badge.update()

    def _chat_event_content(self, event: ChatEvent) -> str:
        if event.embed is not None:
            description: str = event.embed.description.strip()
            if description:
                return description
        app: object | None = self._chat_room_app(event.room_id)
        friendly_value: object = getattr(app, "friendly", event.room_id) if app is not None else event.room_id
        app_friendly: str = friendly_value if isinstance(friendly_value, str) and friendly_value else event.room_id
        return event.render_content(app_name=app_friendly)

    def _chat_event_display_content(self, event: ChatEvent) -> str:
        if self._chat_event_hides_body_content(event):
            return ""
        return self._chat_event_content(event)

    def _chat_event_copy_text(self, event: ChatEvent) -> str:
        content: str = self._chat_event_content(event).strip()
        if content:
            return content
        reference = event.reference
        if reference is None:
            return ""
        return reference.content.strip()

        ui.notify("Copied message text.", type="positive")

    @staticmethod
    def _chat_event_hides_body_content(event: ChatEvent) -> bool:
        notice = event.resolved_notice()
        if notice is None:
            return False
        return notice_hides_body_content(notice)

    @classmethod
    def _chat_event_badges(cls, event: ChatEvent) -> tuple[_ModWebBadgeSpec, ...]:
        notice = event.resolved_notice()
        if notice is not None:
            badges: list[_ModWebBadgeSpec] = []
            notice_badge = notice_badge_spec(notice)
            if notice_badge is not None:
                badges.append(_ModWebBadgeSpec(text=notice_badge.text, tone=notice_badge.tone))
            for extra_badge in notice_additional_badge_specs(notice):
                badges.append(_ModWebBadgeSpec(text=extra_badge.text, tone=extra_badge.tone))
            return tuple(badges)
        embed: ChatEmbed | None = event.embed
        if embed is None:
            return ()
        badge = cls._chat_event_badge_for_label(embed.title)
        if badge is None:
            return ()
        return (badge,)

    @staticmethod
    def _chat_event_badge_for_label(label: str) -> _ModWebBadgeSpec | None:
        badge = relay_notice_badge_spec_from_label(label)
        if badge is None:
            return None
        return _ModWebBadgeSpec(text=badge.text, tone=badge.tone)

    @staticmethod
    def _chat_author_color_hex(event: ChatEvent) -> str:
        return event.author.color_hex or DEFAULT_CHAT_AUTHOR_COLOR_HEX

    @staticmethod
    def _chat_author_avatar_uri(event: ChatEvent) -> str | None:
        avatar_uri: str | None = event.author.avatar_uri
        if avatar_uri is None:
            return None
        normalised_avatar_uri: str = avatar_uri.strip()
        if normalised_avatar_uri.startswith(("http://", "https://")):
            return normalised_avatar_uri
        if re.fullmatch(r"data:image/(?:png|svg\+xml);base64,[A-Za-z0-9+/=]+", normalised_avatar_uri):
            return normalised_avatar_uri
        return None

    def _render_chat_author_avatar(self, *, ui: ModWebUi, event: ChatEvent) -> None:
        avatar_uri: str | None = self._chat_author_avatar_uri(event)
        if avatar_uri is None:
            return
        avatar_alt: str = f"{self._chat_event_author_display_name(event)} avatar"
        ui.html(
            (
                "<img"
                f' class="mod-chat-author-avatar" src="{escape(avatar_uri, quote=True)}"'
                f' alt="{escape(avatar_alt, quote=True)}" loading="lazy" referrerpolicy="no-referrer">'
            )
        )

    @staticmethod
    def _chat_app_status_badge(app_stats: NodeAppRuntimeSummary | None) -> tuple[str, BadgeTone]:
        if app_stats is None:
            return "Status unknown", "warn"
        if app_stats.transition_state is NodeAppTransitionState.STARTING:
            return "Starting", "purple"
        if app_stats.transition_state is NodeAppTransitionState.STOPPING:
            return "Stopping", "warn"
        if app_stats.running:
            return "Running", "purple"
        if not app_stats.enabled:
            return "Disabled", "red"
        if app_stats.runtime_fault is not None:
            return "Crashed", "red"
        return "Stopped", "grey"

    def _chat_event_source_label(self, event: ChatEvent, *, room_id: str | None = None) -> str:
        if event.author.kind is ChatAuthorKind.SYSTEM:
            return "SYSTEM"
        if event.source.kind is ChatEndpointKind.APP:
            return self._app_chat_event_source_label(event, room_id=room_id)
        if event.source.kind is ChatEndpointKind.DISCORD_CHANNEL:
            return self._discord_chat_event_source_label(event)
        if event.source.kind is ChatEndpointKind.WEB_SESSION:
            return "WEB"
        return event.source.kind.value.replace("_", " ").title()

    def _app_chat_event_source_label(self, event: ChatEvent, *, room_id: str | None = None) -> str:
        source_room_id = event.source.value
        current_room_id = room_id or event.room_id
        if source_room_id.casefold() == current_room_id.casefold():
            return "GAME"
        return self._chat_room_label(source_room_id)

    def _chat_room_label(self, room_id: str) -> str:
        app: object | None = self._chat_room_app(room_id)
        if app is None:
            return room_id
        friendly = getattr(app, "friendly", None)
        if isinstance(friendly, str) and friendly.strip():
            return friendly.strip()
        return room_id

    def _discord_chat_event_source_label(self, event: ChatEvent) -> str:
        guild_name: str | None = self._discord_chat_event_guild_name(event)
        channel_name: str | None = self._discord_chat_event_channel_name(event)
        if guild_name is None:
            return channel_name or event.source_label or "Discord"
        if self._discord_guild_has_multiple_room_channels(event.room_id, guild_id=event.source_guild_id):
            if channel_name is not None:
                return f"{guild_name}.{channel_name}"
        return guild_name

    def _discord_chat_event_guild_name(self, event: ChatEvent) -> str | None:
        if event.source_guild_name is not None and event.source_guild_name.strip():
            return event.source_guild_name
        guild_id: int | None = event.source_guild_id
        if guild_id is None:
            source_channel: object | None = self._discord_chat_source_channel(event)
            cached_guild_id = getattr(source_channel, "guild_id", None)
            if isinstance(cached_guild_id, int | str):
                guild_id = int(cached_guild_id)
        if guild_id is None:
            return None
        manager: App_Manager | None = self._manager
        if manager is None:
            return None
        manager_object: object = manager
        bot: object | None = getattr(manager_object, "bot", None)
        cache: object | None = getattr(bot, "cache", None) if bot is not None else None
        get_guild_candidate: object | None = getattr(cache, "get_guild", None) if cache is not None else None
        get_guild = cast(
            Callable[[int], object | None] | None,
            get_guild_candidate if callable(get_guild_candidate) else None,
        )
        guild: object | None = get_guild(guild_id) if get_guild is not None else None
        guild_name = getattr(guild, "name", None)
        if isinstance(guild_name, str) and guild_name.strip():
            return guild_name
        return None

    def _discord_chat_event_channel_name(self, event: ChatEvent) -> str | None:
        source_channel: object | None = self._discord_chat_source_channel(event)
        channel_name = getattr(source_channel, "name", None)
        if isinstance(channel_name, str) and channel_name.strip():
            return channel_name
        if event.source_label is not None and event.source_label.strip():
            return event.source_label
        return None

    def _discord_chat_source_channel(self, event: ChatEvent) -> object | None:
        return self._discord_chat_source_channel_by_id(event.source_channel_id)

    def _discord_chat_source_channel_by_id(self, channel_id: int | None) -> object | None:
        if channel_id is None:
            return None
        manager = self._manager
        if manager is None:
            return None
        manager_object: object = manager
        bot: object | None = getattr(manager_object, "bot", None)
        cache: object | None = getattr(bot, "cache", None) if bot is not None else None
        get_guild_channel_candidate: object | None = (
            getattr(cache, "get_guild_channel", None) if cache is not None else None
        )
        get_guild_channel = cast(
            Callable[[int], object | None] | None,
            get_guild_channel_candidate if callable(get_guild_channel_candidate) else None,
        )
        if get_guild_channel is not None:
            channel: object | None = get_guild_channel(channel_id)
            if channel is not None:
                return channel
        return None

    def _discord_chat_channel_name_by_id(self, channel_id: int) -> str | None:
        channel = self._discord_chat_source_channel_by_id(channel_id)
        channel_name = getattr(channel, "name", None)
        if isinstance(channel_name, str) and channel_name.strip():
            return channel_name
        return None

    def _discord_role_name_by_id(self, role_id: int) -> str | None:
        manager = self._manager
        if manager is None:
            return None
        manager_object: object = manager
        bot: object | None = getattr(manager_object, "bot", None)
        cache: object | None = getattr(bot, "cache", None) if bot is not None else None
        get_role_candidate: object | None = getattr(cache, "get_role", None) if cache is not None else None
        get_role = cast(
            Callable[[int], object | None] | None,
            get_role_candidate if callable(get_role_candidate) else None,
        )
        if get_role is None:
            return None
        role = get_role(role_id)
        role_name = getattr(role, "name", None)
        if isinstance(role_name, str) and role_name.strip():
            return role_name
        return None

    @staticmethod
    def _discord_relative_timestamp_text(*, unix_timestamp: int) -> str:
        delta_seconds = unix_timestamp - int(time.time())
        absolute_delta_seconds = abs(delta_seconds)
        if absolute_delta_seconds < 60:
            amount = absolute_delta_seconds
            unit = "second"
        elif absolute_delta_seconds < 3600:
            amount = absolute_delta_seconds // 60
            unit = "minute"
        elif absolute_delta_seconds < 86400:
            amount = absolute_delta_seconds // 3600
            unit = "hour"
        else:
            amount = absolute_delta_seconds // 86400
            unit = "day"
        suffix = "" if amount == 1 else "s"
        if delta_seconds >= 0:
            return f"in {amount} {unit}{suffix}"
        return f"{amount} {unit}{suffix} ago"

    def _discord_guild_has_multiple_room_channels(self, room_id: str, *, guild_id: int | None) -> bool:
        if guild_id is None:
            return False
        app: object | None = self._chat_room_app(room_id)
        if app is None:
            return False
        channel_ids: tuple[int, ...] = self._chat_room_channel_ids(app)
        if len(channel_ids) < 2:
            return False

        matching_channel_count = 0
        for channel_id in channel_ids:
            channel: object | None = self._discord_chat_source_channel_by_id(channel_id)
            channel_guild_id = getattr(channel, "guild_id", None)
            if isinstance(channel_guild_id, int | str) and int(channel_guild_id) == guild_id:
                matching_channel_count += 1
                if matching_channel_count > 1:
                    return True
        return False

    def _chat_room_app(self, room_id: str) -> object | None:
        manager: App_Manager | None = self._manager
        if manager is None:
            return None
        app = manager.apps.get(room_id)
        if app is not None:
            return app
        try:
            return manager.get(room_id)
        except Exception:
            return None

    def _chat_room_scope(self, room_id: str) -> str | None:
        app: object | None = self._chat_room_app(room_id)
        scope = getattr(app, "scope", None) if app is not None else None
        if isinstance(scope, str) and scope.strip():
            return scope
        return None

    def _chat_room_platforms(self, room_id: str) -> tuple[str, ...]:
        app: object | None = self._chat_room_app(room_id)
        raw_platforms = getattr(app, "name_platforms", ()) if app is not None else ()
        if not isinstance(raw_platforms, tuple | list):
            return ()
        return tuple(str(platform).strip().lower() for platform in raw_platforms if str(platform).strip())

    def _chat_room_preferred_platform(self, room_id: str) -> str | None:
        app: object | None = self._chat_room_app(room_id)
        preferred_platform = getattr(app, "preferred_name_platform", None) if app is not None else None
        if not isinstance(preferred_platform, str):
            return None
        value = preferred_platform.strip().lower()
        return value or None

    @staticmethod
    def _chat_room_channel_ids(app: object) -> tuple[int, ...]:
        raw_channels: object = getattr(app, "chat_channels", ())
        channel_ids: list[int] = []
        seen_channel_ids: set[int] = set[int]()
        if isinstance(raw_channels, tuple | list | set | frozenset):
            for channel_id in cast(Iterable[object], raw_channels):
                if not isinstance(channel_id, int | str):
                    continue
                resolved_channel_id: int = int(channel_id)
                if resolved_channel_id in seen_channel_ids:
                    continue
                channel_ids.append(resolved_channel_id)
                seen_channel_ids.add(resolved_channel_id)
        if channel_ids:
            return tuple[int, ...](channel_ids)
        raw_channel: object | None = getattr(app, "chat_channel", None)
        if raw_channel is None:
            return ()
        if not isinstance(raw_channel, int | str):
            return ()
        return (int(raw_channel),)

    @staticmethod
    def _chat_event_tone(event: ChatEvent) -> BadgeTone:
        if event.author.kind is ChatAuthorKind.SYSTEM:
            return "warn"
        if event.source.kind is ChatEndpointKind.APP:
            return "purple"
        if event.source.kind is ChatEndpointKind.DISCORD_CHANNEL:
            return "black"
        if event.source.kind is ChatEndpointKind.WEB_SESSION:
            return "grey"
        return "warn"

    @staticmethod
    def _chat_event_source_class(event: ChatEvent) -> str:
        if event.author.kind is ChatAuthorKind.SYSTEM:
            return "system"
        if event.source.kind is ChatEndpointKind.APP:
            return "game"
        if event.source.kind is ChatEndpointKind.DISCORD_CHANNEL:
            return "discord"
        if event.source.kind is ChatEndpointKind.WEB_SESSION:
            return "web"
        return "unknown"
