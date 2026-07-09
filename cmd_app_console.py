from __future__ import annotations

from collections.abc import Mapping

import hikari
import lightbulb
from hikari_ui import (
    Editor,
    EditorButton,
    EditorLayout,
    EditorPageState,
    EditorRequest,
    EditorResponse,
    EditorSelectOption,
    InteractionDeferral,
    ModalKit,
    ModalRequest,
    ModalSchema,
    ModalTextField,
    PagedActionCodec,
)

import _errors
from _editor_session import startup_editor_prefix
from _manager import App_Manager
from _security import Access_Control
from apps._app import App
from apps._console import ConsoleAction, ConsoleActionParameter, ConsoleActionResult, execute_console_action
from cmd_app_manage import (
    APP_CONSOLE_ACTION_LEVELS,
    AppConsoleActionKind,
    AppConsoleState,
    AppManageService,
    ConsoleActionExecutionView,
    ConsoleActionView,
    EditorStatus,
    _APP_CONSOLE_MODAL_PREFIX,
    _APP_CONSOLE_PREFIX,
    _APP_CONSOLE_VALUE_FIELD_ID,
    _EMBED_SPACER,
    _EMBED_SUBTEXT,
    _PAGE_SIZE,
    _app_status_lines,
    _app_summary_line,
    _coerce_status,
    _component_text,
    _console_action_allows_modal_entry,
    _console_action_choice_items,
    _console_action_option_description,
    _console_action_recent_items,
    _console_action_recent_value_at,
    _console_action_result_status_text,
    _console_action_status_lines_for_view,
    _console_action_supports_choice_select,
    _console_action_supports_recent_select,
    _console_state_from_value,
    _console_state_value,
    _display_value,
    _editor_title,
    _error_status,
    _page_for_item_index,
    _paginate,
    _public_console_action_text,
    _send_public_action_notice,
    _status_text,
)


async def ac_console_apps(ctx: lightbulb.AutocompleteContext[str], manager: App_Manager) -> None:
    await ctx.respond([app.friendly for app in manager.apps.values() if app.supports_console_actions])


class AppConsoleService:
    def __init__(self) -> None:
        self._action_codec = PagedActionCodec(AppConsoleActionKind)
        self._editor = Editor(
            prefix=startup_editor_prefix(_APP_CONSOLE_PREFIX),
            on_action=self._on_editor_action,
            authoriser=self._authorise_editor_action,
        )
        self._value_modal_prefix = startup_editor_prefix(_APP_CONSOLE_MODAL_PREFIX)

    async def open_editor(
        self,
        *,
        ctx: lightbulb.Context,
        acl: Access_Control,
        manager: App_Manager,
        app: App,
        status: EditorStatus | str | None = None,
    ) -> None:
        if not app.supports_console_actions:
            raise _errors.UnsupportedConsole(f"{app.friendly} does not support console actions")
        state = AppConsoleState(page=0, app_name=app.name)
        resolved_status = _coerce_status(status)
        if resolved_status is None:
            resolved_status = EditorStatus(text=f"Opened console actions for {app.friendly}.")
        status_text = _status_text(resolved_status)
        assert status_text is not None
        embed, components = self._render_editor(
            actor_user_id=int(ctx.user.id),
            locale=self._editor.resolve_locale(ctx.interaction),
            acl=acl,
            manager=manager,
            state=state,
            status=resolved_status,
        )
        await ctx.respond(
            status_text,
            embed=embed,
            components=components,
            flags=hikari.MessageFlag.EPHEMERAL,
        )

    async def route_component(
        self,
        interaction: hikari.ComponentInteraction,
        *,
        bot: hikari.GatewayBot,
        acl: Access_Control,
        manager: App_Manager,
    ) -> bool:
        return await self._editor.route(interaction, bot=bot, acl=acl, manager=manager)

    async def route_modal(
        self,
        interaction: hikari.ModalInteraction,
        *,
        bot: hikari.GatewayBot,
        acl: Access_Control,
        manager: App_Manager,
    ) -> bool:
        routed_modal = self._resolve_value_modal(interaction, manager=manager)
        if routed_modal is None:
            return False
        return await routed_modal.route(
            interaction,
            on_submit=self._on_modal_submit,
            authoriser=self._authorise_modal_submit,
            defer_resolver=self._defer_modal_submit,
            unauthorised_message="You are not authorised to run this console action.",
            invalid_message="Console action input is invalid.",
            bot=bot,
            acl=acl,
            manager=manager,
        )

    async def _authorise_editor_action(self, req: EditorRequest, deps: Mapping[str, object]) -> bool:
        acl = AppManageService._require_acl(deps)
        manager = AppManageService._require_manager(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return False
        required_level = APP_CONSOLE_ACTION_LEVELS.get(action.kind)
        if required_level is None or not acl.can(int(req.user_id), required_level):
            return False
        if action.kind not in {
            AppConsoleActionKind.EXECUTE_ACTION,
            AppConsoleActionKind.OPEN_ACTION_MODAL,
            AppConsoleActionKind.REUSE_ACTION,
        }:
            return True
        state = _console_state_from_value(action.value, action.page)
        if state is None:
            return False
        target_action = self._selected_action(manager=manager, state=state)
        if target_action is None:
            return False
        return acl.can(int(req.user_id), target_action.power_level)

    async def _authorise_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> bool:
        acl = AppManageService._require_acl(deps)
        manager = AppManageService._require_manager(deps)
        action = self._action_codec.parse(req.action)
        if action is None or action.kind is not AppConsoleActionKind.EXECUTE_ACTION:
            return False
        state = _console_state_from_value(action.value, action.page)
        if state is None:
            return False
        target_action = self._selected_action(manager=manager, state=state)
        if target_action is None:
            return False
        return acl.can(int(req.user_id), target_action.power_level)

    def _defer_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> InteractionDeferral | None:
        del deps
        action = self._action_codec.parse(req.action)
        if action is None or action.kind is not AppConsoleActionKind.EXECUTE_ACTION:
            return None
        return InteractionDeferral.update()

    async def _on_editor_action(self, req: EditorRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown app console action.")
        if action.kind is AppConsoleActionKind.CLOSE:
            return EditorResponse.close("App console closed.")

        manager = AppManageService._require_manager(deps)
        acl = AppManageService._require_acl(deps)
        bot = AppManageService._require_bot(deps)
        state = _console_state_from_value(action.value, action.page)
        if state is None:
            return EditorResponse.ephemeral("App console state is invalid.")

        actor_user_id = int(req.user_id)
        try:
            app = manager.get(state.app_name)
        except ValueError as xcp:
            return EditorResponse.ephemeral(str(xcp))
        if not app.supports_console_actions:
            return EditorResponse.ephemeral(f"{app.friendly} does not support console actions.")
        if action.kind is AppConsoleActionKind.PAGE:
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status="Page updated.",
            )
        if action.kind is AppConsoleActionKind.REFRESH:
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status=f"Refreshed console actions for {app.friendly}.",
            )
        if action.kind is AppConsoleActionKind.SELECT_ACTION:
            if not req.values:
                return EditorResponse.ephemeral("Choose an action first.")
            view = self._build_console_view(app=app, state=state)
            selected_index = next(
                (index for index, item in enumerate(view.actions.visible) if item.key == req.values[0]),
                None,
            )
            if selected_index is None:
                return EditorResponse.ephemeral("Choose an action from the current page.")
            absolute_index = view.actions.page_state.page * _PAGE_SIZE + selected_index
            next_state = AppConsoleState(
                page=view.actions.page_state.page,
                app_name=app.name,
                selected_action_index=absolute_index,
            )
            selected_action = app.console_actions[absolute_index]
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=next_state,
                status=f"Selected {selected_action.label}.",
            )

        selected_action = self._selected_action(manager=manager, state=state)
        if selected_action is None:
            return EditorResponse.ephemeral("Choose an action first.")

        if action.kind is AppConsoleActionKind.OPEN_ACTION_MODAL:
            if not _console_action_allows_modal_entry(selected_action):
                return EditorResponse.ephemeral("This action must be run using its available choices.")
            parameter = selected_action.parameter
            if parameter is None:
                return EditorResponse.ephemeral("This action does not require input.")
            value_modal = self._build_value_modal(parameter)
            await req.interaction.create_modal_response(
                f"Run {selected_action.label}",
                value_modal.build_id(
                    self._build_state_action(AppConsoleActionKind.EXECUTE_ACTION, state),
                    scope_id=actor_user_id,
                    user_id=actor_user_id,
                ),
                components=value_modal.rows({_APP_CONSOLE_VALUE_FIELD_ID: ""}),
            )
            return None

        if action.kind is AppConsoleActionKind.EXECUTE_ACTION:
            raw_value = req.values[0] if req.values else None
            execution = await self._execute_action_for_view(
                app=app,
                action=selected_action,
                raw_value=raw_value,
            )
            response = self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status=execution.status,
            )
            if execution.success:
                await _send_public_action_notice(
                    bot,
                    req.interaction,
                    _public_console_action_text(
                        actor_user_id=actor_user_id,
                        app=app,
                        action=selected_action,
                        raw_value=raw_value,
                    ),
                )
            return response
        if action.kind is AppConsoleActionKind.REUSE_ACTION:
            if not req.values:
                return EditorResponse.ephemeral("Choose a recent value first.")
            raw_value = _console_action_recent_value_at(selected_action, req.values[0])
            if raw_value is None:
                return EditorResponse.ephemeral("Choose a recent value from the current list.")
            execution = await self._execute_action_for_view(
                app=app,
                action=selected_action,
                raw_value=raw_value,
            )
            response = self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status=execution.status,
            )
            if execution.success:
                await _send_public_action_notice(
                    bot,
                    req.interaction,
                    _public_console_action_text(
                        actor_user_id=actor_user_id,
                        app=app,
                        action=selected_action,
                        raw_value=raw_value,
                    ),
                )
            return response
        return EditorResponse.ephemeral("Unsupported app console action.")

    async def _on_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        manager = AppManageService._require_manager(deps)
        acl = AppManageService._require_acl(deps)
        bot = AppManageService._require_bot(deps)
        action = self._action_codec.parse(req.action)
        if action is None or action.kind is not AppConsoleActionKind.EXECUTE_ACTION:
            return EditorResponse.ephemeral("Unknown app console modal action.")
        state = _console_state_from_value(action.value, action.page)
        if state is None:
            return EditorResponse.ephemeral("App console state is invalid.")
        try:
            app = manager.get(state.app_name)
        except ValueError as xcp:
            return EditorResponse.ephemeral(str(xcp))
        selected_action = self._selected_action(manager=manager, state=state)
        if selected_action is None:
            return EditorResponse.ephemeral("Choose an action first.")
        raw_value = req.values.get(_APP_CONSOLE_VALUE_FIELD_ID, "").strip()
        execution = await self._execute_action_for_view(
            app=app,
            action=selected_action,
            raw_value=raw_value,
        )
        response = self._build_editor_response(
            actor_user_id=int(req.user_id),
            locale=self._editor.resolve_locale(req.interaction),
            acl=acl,
            manager=manager,
            state=state,
            status=execution.status,
        )
        if execution.success:
            await _send_public_action_notice(
                bot,
                req.interaction,
                _public_console_action_text(
                    actor_user_id=int(req.user_id),
                    app=app,
                    action=selected_action,
                    raw_value=raw_value,
                ),
            )
        return response

    async def _execute_action(
        self,
        *,
        app: App,
        action: ConsoleAction,
        raw_value: str | None,
    ) -> ConsoleActionResult:
        return await execute_console_action(app=app, is_running=app.check_running, action=action, raw_value=raw_value)

    async def _execute_action_for_view(
        self,
        *,
        app: App,
        action: ConsoleAction,
        raw_value: str | None,
    ) -> ConsoleActionExecutionView:
        try:
            result = await self._execute_action(app=app, action=action, raw_value=raw_value)
        except Exception as xcp:
            return ConsoleActionExecutionView(
                status=_error_status(f"Error: console action failed for `{action.label}`: {xcp}"),
                success=False,
            )
        return ConsoleActionExecutionView(
            status=EditorStatus(text=_console_action_result_status_text(result), is_error=not result.success),
            success=result.success,
        )

    def _build_value_modal(self, parameter: ConsoleActionParameter[object]) -> ModalKit:
        placeholder = None
        if parameter.desc:
            placeholder = _component_text(parameter.desc, limit=100)
        return ModalKit(
            prefix=self._value_modal_prefix,
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_APP_CONSOLE_VALUE_FIELD_ID,
                        label=parameter.label,
                        style=hikari.TextInputStyle.PARAGRAPH if parameter.multiline else hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=parameter.max_length,
                        placeholder=placeholder,
                    )
                ]
            ),
        )

    def _resolve_value_modal(
        self,
        interaction: hikari.ModalInteraction,
        *,
        manager: App_Manager,
    ) -> ModalKit | None:
        modal = ModalKit(prefix=self._value_modal_prefix)
        parsed = modal.parse_id(interaction.custom_id)
        if parsed is None:
            return None
        action = self._action_codec.parse(parsed.value)
        if action is None or action.kind is not AppConsoleActionKind.EXECUTE_ACTION:
            return None
        state = _console_state_from_value(action.value, action.page)
        if state is None:
            return None
        selected_action = self._selected_action(manager=manager, state=state)
        if selected_action is None or selected_action.parameter is None:
            return None
        return self._build_value_modal(selected_action.parameter)

    def _selected_action(self, *, manager: App_Manager, state: AppConsoleState) -> ConsoleAction | None:
        try:
            app = manager.get(state.app_name)
        except ValueError:
            return None
        if state.selected_action_index is None:
            return None
        actions = app.console_actions
        if state.selected_action_index >= len(actions):
            return None
        return actions[state.selected_action_index]

    def _state_for_action(self, *, app: App, action: ConsoleAction) -> AppConsoleState:
        selected_action_index = next(
            (index for index, candidate in enumerate(app.console_actions) if candidate.key == action.key),
            None,
        )
        if selected_action_index is None:
            raise ValueError(f"Unknown console action key for {app.name}: {action.key}")
        return AppConsoleState(
            page=_page_for_item_index(selected_action_index),
            app_name=app.name,
            selected_action_index=selected_action_index,
        )

    def _build_console_view(self, *, app: App, state: AppConsoleState) -> ConsoleActionView:
        actions = _paginate(app.console_actions, state.page)
        selected_action_slot = None
        selected_action = None
        if state.selected_action_index is not None:
            page_start = actions.page_state.page * _PAGE_SIZE
            page_end = page_start + len(actions.visible)
            if page_start <= state.selected_action_index < page_end:
                selected_action_slot = state.selected_action_index - page_start
                selected_action = actions.visible[selected_action_slot]
        return ConsoleActionView(
            actions=actions,
            selected_action=selected_action,
            selected_action_slot=selected_action_slot,
        )

    def _build_state_action(self, kind: AppConsoleActionKind, state: AppConsoleState) -> str:
        return self._action_codec.build(kind, page=state.page, value=_console_state_value(state))

    def _build_editor_response(
        self,
        *,
        actor_user_id: int,
        locale: hikari.Locale,
        acl: Access_Control,
        manager: App_Manager,
        state: AppConsoleState,
        status: EditorStatus | str,
    ) -> EditorResponse:
        resolved_status = _coerce_status(status)
        status_text = _status_text(resolved_status)
        assert status_text is not None
        embed, components = self._render_editor(
            actor_user_id=actor_user_id,
            locale=locale,
            acl=acl,
            manager=manager,
            state=state,
            status=resolved_status,
        )
        return EditorResponse.update(status_text, embeds=[embed], components=components)

    def _render_editor(
        self,
        *,
        actor_user_id: int,
        locale: hikari.Locale,
        acl: Access_Control,
        manager: App_Manager,
        state: AppConsoleState,
        status: EditorStatus | str | None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        app = manager.get(state.app_name)
        if not app.supports_console_actions:
            raise _errors.UnsupportedConsole(f"{app.friendly} does not support console actions")
        editor_ctx = self._editor.context(scope_id=actor_user_id, user_id=actor_user_id, locale=locale)
        layout = EditorLayout(editor_ctx)
        view = self._build_console_view(app=app, state=state)
        selected_action = view.selected_action
        resolved_status = _coerce_status(status)
        embed = hikari.Embed(
            title=_editor_title(f"{app.friendly} Console", status=resolved_status),
            color=app.manage_embed_color,
        )
        embed.description = "\n".join(
            [
                _app_summary_line(app),
                f"{_EMBED_SUBTEXT}Curated console actions for this app.",
            ]
        )
        embed.add_field(name="App", value=_display_value(_app_status_lines(app)), inline=True)
        embed.add_field(name="Actions", value=f"{view.actions.total_count} available", inline=True)
        if selected_action is not None:
            embed.add_field(
                name="Selected Action",
                value=_display_value(_console_action_status_lines_for_view(selected_action)),
                inline=False,
            )
        else:
            embed.add_field(name=_EMBED_SPACER, value=_EMBED_SPACER, inline=False)

        if view.actions.visible:
            layout.add_text_select(
                self._build_state_action(
                    AppConsoleActionKind.SELECT_ACTION,
                    AppConsoleState(page=view.actions.page_state.page, app_name=app.name),
                ),
                options=[
                    EditorSelectOption(
                        label=_component_text(action.label),
                        value=action.key,
                        description=_component_text(_console_action_option_description(action)),
                    )
                    for action in view.actions.visible
                ],
                placeholder="Choose a console action",
            )

        if selected_action is not None and _console_action_supports_choice_select(selected_action):
            assert selected_action.parameter is not None
            layout.add_text_select(
                self._build_state_action(
                    AppConsoleActionKind.EXECUTE_ACTION, self._state_for_action(app=app, action=selected_action)
                ),
                options=[
                    EditorSelectOption(
                        label=_component_text(label),
                        value=label,
                        description=_component_text(raw_value),
                    )
                    for label, raw_value in _console_action_choice_items(selected_action.parameter)
                ],
                placeholder=f"Run {selected_action.label} with a preset value",
            )

        if selected_action is not None and _console_action_supports_recent_select(selected_action):
            layout.add_text_select(
                self._build_state_action(
                    AppConsoleActionKind.REUSE_ACTION, self._state_for_action(app=app, action=selected_action)
                ),
                options=[
                    EditorSelectOption(
                        label=_component_text(value),
                        value=str(index),
                        description="Recent value",
                    )
                    for index, value in enumerate(_console_action_recent_items(selected_action.parameter))
                ],
                placeholder=f"Reuse a recent value for {selected_action.label}",
            )

        can_run_selected = selected_action is not None and acl.can(actor_user_id, selected_action.power_level)
        buttons: list[EditorButton] = []
        if selected_action is not None:
            if selected_action.parameter is None:
                buttons.append(
                    EditorButton(
                        self._build_state_action(
                            AppConsoleActionKind.EXECUTE_ACTION, self._state_for_action(app=app, action=selected_action)
                        ),
                        "Run Action",
                        style=hikari.ButtonStyle.PRIMARY,
                        is_disabled=not can_run_selected,
                    )
                )
            elif _console_action_allows_modal_entry(selected_action):
                buttons.append(
                    EditorButton(
                        self._build_state_action(
                            AppConsoleActionKind.OPEN_ACTION_MODAL,
                            self._state_for_action(app=app, action=selected_action),
                        ),
                        "Enter Value",
                        style=hikari.ButtonStyle.PRIMARY,
                        is_disabled=not can_run_selected,
                    )
                )
        if buttons:
            layout.add_buttons(*buttons)

        prev_action = None
        next_action = None
        if view.actions.page_state.total_pages > 1:
            prev_action = self._build_state_action(
                AppConsoleActionKind.PAGE,
                AppConsoleState(
                    page=max(0, view.actions.page_state.page - 1),
                    app_name=app.name,
                    selected_action_index=state.selected_action_index,
                ),
            )
            next_action = self._build_state_action(
                AppConsoleActionKind.PAGE,
                AppConsoleState(
                    page=min(view.actions.page_state.total_pages - 1, view.actions.page_state.page + 1),
                    app_name=app.name,
                    selected_action_index=state.selected_action_index,
                ),
            )
        layout.page_footer(
            self._action_codec.build(AppConsoleActionKind.CLOSE, page=view.actions.page_state.page),
            page_state=EditorPageState(
                page=view.actions.page_state.page, total_pages=view.actions.page_state.total_pages
            ),
            prev_action=prev_action,
            next_action=next_action,
            extra_buttons=(
                EditorButton(
                    self._build_state_action(
                        AppConsoleActionKind.REFRESH,
                        AppConsoleState(
                            page=view.actions.page_state.page,
                            app_name=app.name,
                            selected_action_index=state.selected_action_index,
                        ),
                    ),
                    "Refresh",
                    style=hikari.ButtonStyle.SECONDARY,
                    is_disabled=not acl.can(actor_user_id, APP_CONSOLE_ACTION_LEVELS[AppConsoleActionKind.REFRESH]),
                ),
            ),
        )
        return embed, layout.build()
