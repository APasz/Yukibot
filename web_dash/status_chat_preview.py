"""StatusChatPreview UI helpers."""

from __future__ import annotations

from typing import Protocol, cast

# ruff: noqa: F403, F405
from .status_support import *


@dataclass(frozen=True, slots=True)
class _FakeChatMessageModeSpec:
    label: str
    help_text: str


class _FakeChatPreviewStatusSupport(Protocol):
    """Status-service operations supplied by sibling mixins."""

    def _user_can_use_fake_chat_preview(self, user: ModWebUser) -> bool: ...

    def _fake_chat_preview_notice_source(self, source_kind: ChatEndpointKind) -> RelayNoticeSource: ...


_FAKE_CHAT_MESSAGE_MODE_SPECS: Mapping[_ModWebFakeChatMessageMode, _FakeChatMessageModeSpec] = {
    _ModWebFakeChatMessageMode.TEXT: _FakeChatMessageModeSpec(
        label="Text",
        help_text="Freeform chat message with optional reply, link, and attachment preview.",
    ),
    _ModWebFakeChatMessageMode.JOIN: _FakeChatMessageModeSpec(
        label="Join",
        help_text="Player session notice. Body hides in chat and becomes a Joined badge.",
    ),
    _ModWebFakeChatMessageMode.LEAVE: _FakeChatMessageModeSpec(
        label="Leave",
        help_text="Player session notice. Body hides in chat and becomes a Left badge.",
    ),
    _ModWebFakeChatMessageMode.DEATH: _FakeChatMessageModeSpec(
        label="Death",
        help_text="Player death notice with a cause string.",
    ),
    _ModWebFakeChatMessageMode.PVP_KILL: _FakeChatMessageModeSpec(
        label="PVP Kill",
        help_text="PVP death notice with a killer name or detail string.",
    ),
    _ModWebFakeChatMessageMode.ADVANCEMENT: _FakeChatMessageModeSpec(
        label="Advancement",
        help_text="Game progress notice with badge and embed rendering.",
    ),
    _ModWebFakeChatMessageMode.GOAL: _FakeChatMessageModeSpec(
        label="Goal",
        help_text="Goal progress notice with badge and embed rendering.",
    ),
    _ModWebFakeChatMessageMode.CHALLENGE: _FakeChatMessageModeSpec(
        label="Challenge",
        help_text="Challenge progress notice with badge and embed rendering.",
    ),
    _ModWebFakeChatMessageMode.RESEARCH: _FakeChatMessageModeSpec(
        label="Research",
        help_text="Research progress notice with badge and embed rendering.",
    ),
    _ModWebFakeChatMessageMode.GAME_EVENT: _FakeChatMessageModeSpec(
        label="Game Event",
        help_text="Generic game event notice with a custom label and detail.",
    ),
    _ModWebFakeChatMessageMode.APP_STARTED: _FakeChatMessageModeSpec(
        label="App Started",
        help_text="App lifecycle started notice using detail text as join address.",
    ),
    _ModWebFakeChatMessageMode.APP_STOPPED: _FakeChatMessageModeSpec(
        label="App Stopped",
        help_text="App lifecycle stopped notice using secondary text as detail lines.",
    ),
    _ModWebFakeChatMessageMode.APP_CRASHED: _FakeChatMessageModeSpec(
        label="App Crashed",
        help_text="App lifecycle crash notice using detail text as summary.",
    ),
    _ModWebFakeChatMessageMode.MAINTENANCE_WARNING: _FakeChatMessageModeSpec(
        label="Maintenance Warning",
        help_text="Maintenance warning notice using detail text as lead minutes.",
    ),
    _ModWebFakeChatMessageMode.BOT_STARTED: _FakeChatMessageModeSpec(
        label="Bot Started",
        help_text="Bot startup notice using detail text as auto-launch app name.",
    ),
    _ModWebFakeChatMessageMode.BOT_ERROR: _FakeChatMessageModeSpec(
        label="Bot Error",
        help_text="Bot error notice using detail text as the summary.",
    ),
    _ModWebFakeChatMessageMode.EMBED: _FakeChatMessageModeSpec(
        label="Embed",
        help_text="Custom embed message with optional body text, reply, link, and attachment.",
    ),
}
_FAKE_CHAT_MESSAGE_OPTIONS: Mapping[str, _ModWebFakeChatMessageMode] = {
    spec.label: mode for mode, spec in _FAKE_CHAT_MESSAGE_MODE_SPECS.items()
}


class ModWebStatusChatPreviewMixin(ModWebStatusFeatureSupport):
    def _render_fake_chat_preview_control(
        self,
        *,
        ui: ModWebUi,
        user: ModWebUser,
        app_name: str,
        app_friendly: str,
        publish_event: Callable[[ChatEvent], Awaitable[ChatEvent]],
    ) -> None:
        status_support = cast(_FakeChatPreviewStatusSupport, cast(object, self))
        if not status_support._user_can_use_fake_chat_preview(user):
            return
        open_preview = self._build_fake_chat_preview_panel(
            ui=ui,
            app_name=app_name,
            app_friendly=app_friendly,
            publish_event=publish_event,
        )
        ui.button("Fake Chat", on_click=open_preview).classes(
            f"{MOD_WEB_ACTION_BASE_CLASSES} px-4 py-2 text-sm mod-action-border-accent"
        )

    def _build_fake_chat_preview_panel(
        self,
        *,
        ui: ModWebUi,
        app_name: str,
        app_friendly: str,
        publish_event: Callable[[ChatEvent], Awaitable[ChatEvent]],
    ) -> Callable[[], None]:
        target_app_name = app_name.strip()
        if not target_app_name:
            raise ValueError("Fake chat preview app name must not be empty.")
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
        message_options: Mapping[str, _ModWebFakeChatMessageMode] = _FAKE_CHAT_MESSAGE_OPTIONS
        reference_options: dict[str, ChatReferenceKind] = {
            "None": ChatReferenceKind.NONE,
            "Reply": ChatReferenceKind.REPLY,
            "Forward": ChatReferenceKind.FORWARD,
        }
        state = _ModWebFakeChatPreviewState(app_name=target_app_name)
        initial_source_label: str = next(
            label for label, option in source_options.items() if option is state.source_kind
        )
        initial_author_label: str = next(
            label for label, option in author_options.items() if option is state.author_kind
        )
        initial_message_label: str = _FAKE_CHAT_MESSAGE_MODE_SPECS[state.message_mode].label
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

        def _update_reference_kind(value: object) -> None:
            if value is not None:
                option: ChatReferenceKind | None = reference_options.get(str(value).strip())
                if option is not None:
                    state.reference_kind = option
            _refresh_preview()

        def _event_text(value: object) -> str:
            return str(value or "")

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
            try:
                event = self._build_fake_chat_preview_event_for_room(state, room_id=target_app_name)
                await publish_event(event)
            except Exception as xcp:
                log.warning("Fake chat publish failed: app=%s error=%s", target_app_name, xcp)
                ui.notify(f"Fake chat publish failed: {xcp}", type="negative")
                return
            ui.notify(f"Sent fake chat event to {app_friendly}.", type="positive")

        with ui.dialog() as preview_dialog:
            with ui.card().classes("mod-card mod-dialog-card mod-fake-chat-dialog-card"):
                with ui.column().classes("w-full gap-4 p-5"):
                    with ui.column().classes("gap-0"):
                        ui.label(f"Fake Chat · {app_friendly}").classes("text-xl font-black mod-title-small")
                        ui.label("Build a synthetic event for this app's chat relay.").classes("mod-subtitle text-sm")
                        mode_help_label = ui.label(self._fake_chat_preview_mode_help_text(state.message_mode)).classes(
                            "mod-subtitle text-xs"
                        )
                    with ui.grid(columns=2).classes("w-full gap-3"):
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
                        ui.button("Publish Event", on_click=_publish_preview_event).classes("mod-list-button")
                        ui.button("Close", on_click=preview_dialog.close).classes("mod-list-button secondary")
        return preview_dialog.open

    @staticmethod
    def _fake_chat_select_props(*, clearable: bool) -> str:
        clearable_token = " clearable" if clearable else ""
        return (
            f"filled square dense{clearable_token} hide-bottom-space color=accent "
            "options-dense popup-content-class=mod-fake-chat-menu"
        )

    def _build_fake_chat_preview_event(self, state: _ModWebFakeChatPreviewState) -> ChatEvent:
        room_id = state.app_name.strip() if state.app_name is not None else ""
        return self._build_fake_chat_preview_event_for_room(state, room_id=room_id or "preview_room")

    def _build_fake_chat_preview_event_for_room(self, state: _ModWebFakeChatPreviewState, *, room_id: str) -> ChatEvent:
        resolved_room_id = room_id.strip()
        if not resolved_room_id:
            raise ValueError("Fake chat preview room id must not be empty.")
        source: ChatEndpointId = self._fake_chat_preview_source_id(
            source_kind=state.source_kind,
            room_id=resolved_room_id,
        )
        author: ChatAuthor = self._fake_chat_preview_author(state)
        status_support = cast(_FakeChatPreviewStatusSupport, cast(object, self))
        notice_source = status_support._fake_chat_preview_notice_source(state.source_kind)
        app_name: str = self._fake_chat_preview_app_name(room_id=resolved_room_id)
        notice: RelayNotice | None = self._fake_chat_preview_notice(state=state, notice_source=notice_source)
        if notice is not None:
            content = render_notice_text(notice, author_name=author.display_name, app_name=app_name)
            embed = None
        elif state.message_mode is _ModWebFakeChatMessageMode.EMBED:
            embed_title: str = state.embed_title.strip() or "Preview"
            embed_description: str = state.embed_description.strip() or "Preview details"
            content = state.content_text.strip() or f"{embed_title}: {embed_description}"
            embed = ChatEmbed(
                title=embed_title,
                description=embed_description,
                color=self._fake_chat_preview_embed_color(room_id=resolved_room_id),
            )
        else:
            content = state.content_text.strip() or "hello from preview"
            embed = None
        return self._fake_chat_preview_chat_event(
            room_id=resolved_room_id,
            source=source,
            author=author,
            state=state,
            content=content,
            notice=notice,
            embed=embed,
        )

    @staticmethod
    def _fake_chat_preview_mode_help_text(mode: _ModWebFakeChatMessageMode) -> str:
        return _FAKE_CHAT_MESSAGE_MODE_SPECS[mode].help_text

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
        author_name = state.author_name.strip() or state.author_kind.value.replace("_", " ").title()
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
        friendly: object = getattr(app, "friendly", None)
        if isinstance(friendly, str) and friendly.strip():
            return friendly.strip()
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
        color = getattr(app, "manage_embed_color", 0x8B5CF6)
        if isinstance(color, bool) or not isinstance(color, int) or not 0 <= color <= 0xFFFFFF:
            raise ValueError(f"Fake chat preview app colour is invalid for room {room_id!r}.")
        return color

    def _render_app_node_badge(self, *, ui: ModWebUi, node_name: str) -> None:
        with ui.element("div").classes("mod-app-node-badge-wrap"):
            badge: Label = self._badge(ui=ui, text=node_name, tone="black", extra_classes="mod-app-node-badge")
            if color_hex := self._node_role_color_hex(node_name=node_name):
                badge.style(self._node_badge_style(color_hex))
