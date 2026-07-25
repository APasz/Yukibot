"""StatusAliases UI helpers."""

from __future__ import annotations

# ruff: noqa: F403, F405
from .status_support import *


class ModWebStatusAliasesMixin(ModWebStatusFeatureSupport):
    def _alias_known_scopes(self) -> tuple[str, ...]:
        scopes: set[str] = {scope.value for scope in config.AppScopes}
        manager = self._manager
        if manager is None:
            return tuple(sorted(scopes, key=str.casefold))
        list_known_scopes = getattr(manager, "list_known_scopes", None)
        if callable(list_known_scopes):
            known_scopes = list_known_scopes()
            if isinstance(known_scopes, tuple) and all(isinstance(scope, str) for scope in known_scopes):
                scopes.update(scope.strip().lower() for scope in known_scopes if scope.strip())
                return tuple(sorted(scopes, key=str.casefold))
        apps = getattr(manager, "apps", None)
        if isinstance(apps, dict):
            scopes.update(app.scope.strip().lower() for app in apps.values() if app.scope.strip())
        list_create_scopes = getattr(manager, "list_create_scopes", None)
        if callable(list_create_scopes):
            create_scopes = list_create_scopes()
            if isinstance(create_scopes, tuple) and all(isinstance(scope, str) for scope in create_scopes):
                scopes.update(scope.strip().lower() for scope in create_scopes if scope.strip())
        return tuple(sorted(scopes, key=str.casefold))

    @staticmethod
    def _alias_target_label(*, name_cache: config.Name_Cache, user_id: int, viewer: ModWebUser) -> str:
        entry = name_cache.by_id.get(user_id)
        username = entry.account if entry is not None else (viewer.username if user_id == viewer.discord_id else None)
        global_name = entry.global_name if entry is not None else (viewer.global_name if user_id == viewer.discord_id else None)
        primary = global_name or username
        if primary is None:
            return str(user_id)
        return f"{primary} ({user_id})"

    def _alias_target_options(self, *, name_cache: config.Name_Cache, viewer: ModWebUser) -> dict[str, str]:
        user_ids: set[int] = {viewer.discord_id}
        user_ids.update(user_id for user_id in name_cache.by_id if isinstance(user_id, int))
        ordered_user_ids = sorted(
            user_ids,
            key=lambda user_id: self._alias_target_label(name_cache=name_cache, user_id=user_id, viewer=viewer).casefold(),
        )
        return {
            str(user_id): self._alias_target_label(name_cache=name_cache, user_id=user_id, viewer=viewer)
            for user_id in ordered_user_ids
        }

    @staticmethod
    def _parse_manual_alias_target_user_id(value: object) -> int:
        raw_value = str(value).strip()
        if not raw_value.isascii() or not raw_value.isdigit():
            raise ValueError("Manual Discord user ID must contain ASCII digits only.")
        user_id = int(raw_value)
        if not 0 < user_id <= _DISCORD_SNOWFLAKE_MAX:
            raise ValueError("Manual Discord user ID must be a valid positive snowflake.")
        return user_id

    async def _render_alias_page(self, *, ui: ModWebUi, user: ModWebUser, request: Request) -> None:
        self._apply_theme_for_user(ui=ui, user=user)
        ModWebUiHelpersMixin._render_skip_link(ui=ui)
        selected_user_id: int | None = None
        raw_selected_user_id = request.query_params.get("user")
        if raw_selected_user_id and self._user_has_level(user, Power_Level.sudo):
            try:
                selected_user_id = self._parse_manual_alias_target_user_id(raw_selected_user_id)
            except ValueError:
                ui.notify("The requested alias user is invalid; showing your profile instead.", type="warning")

        with ui.column().classes("w-full gap-6 px-4 py-8 md:px-8"):
            with ui.column().classes("mod-page w-full gap-6").props("id=mod-main-content role=main tabindex=-1"):
                self._render_user_header(ui=ui, user=user)
                self._render_alias_page_editor(ui=ui, user=user, selected_user_id=selected_user_id)

    def _render_alias_page_editor(
        self,
        *,
        ui: ModWebUi,
        user: ModWebUser,
        selected_user_id: int | None,
    ) -> None:
        can_switch_user = self._user_has_level(user, Power_Level.sudo)

        def _render_alias_page() -> None:
            name_cache = config.Name_Cache()
            initial_target_user_id = (
                selected_user_id
                if can_switch_user and selected_user_id is not None and selected_user_id in name_cache.by_id
                else user.discord_id
            )
            state: dict[str, int] = {"target_user_id": initial_target_user_id}
            drafts_by_user: dict[int, _AliasDraft] = {}
            form_controls: dict[str, ModWebValueContainer] = {}
            app_alias_controls: dict[str, ModWebValueContainer] = {}
            def _refresh_alias_body() -> None:
                return None

            refresh_alias_body: Callable[[], None] = _refresh_alias_body

            def _target_user_id() -> int:
                target_user_id = state.get("target_user_id", user.discord_id)
                if not can_switch_user:
                    return user.discord_id
                return target_user_id

            def _current_app_scopes() -> tuple[str, ...]:
                return self._alias_known_scopes()

            def _build_alias_draft(target_user_id: int) -> _AliasDraft:
                return _AliasDraft(
                    display_name=name_cache.get_display_override(target_user_id) or "",
                    app_aliases={
                        scope: name_cache.get_game_alias(target_user_id, scope) or "" for scope in _current_app_scopes()
                    },
                    steam_id=name_cache.get_platform_id(target_user_id, "steam") or "",
                    minecraft_uuid=name_cache.get_game_uuid(target_user_id, "minecraft") or "",
                )

            def _draft_for_user(target_user_id: int) -> _AliasDraft:
                draft = drafts_by_user.get(target_user_id)
                if draft is None:
                    draft = _build_alias_draft(target_user_id)
                    drafts_by_user[target_user_id] = draft
                return draft

            def _set_control_value(control: object, value: str) -> None:
                setattr(control, "value", value)

            def _capture_alias_draft() -> None:
                if not form_controls:
                    return
                draft = _draft_for_user(_target_user_id())
                draft.display_name = _value_as_text(form_controls["display_name"])
                draft.add_alias = _value_as_text(form_controls["add_alias"])
                draft.steam_id = _value_as_text(form_controls["steam_id"])
                draft.minecraft_uuid = _value_as_text(form_controls["minecraft_uuid"])
                draft.app_aliases = {
                    scope: _value_as_text(control)
                    for scope, control in app_alias_controls.items()
                }

            async def _save_alias_form() -> None:
                _capture_alias_draft()
                target_user_id = _target_user_id()
                draft = _draft_for_user(target_user_id)
                try:
                    changed_fields = self._persist_alias_draft(
                        name_cache=name_cache,
                        target_user_id=target_user_id,
                        draft=draft,
                        scopes=_current_app_scopes(),
                        sync_authority=False,
                    )
                    if changed_fields:
                        await self._sync_name_cache_with_authority_if_remote_async(name_cache=name_cache)
                except ValueError as xcp:
                    ui.notify(str(xcp), type="negative")
                    return
                except Exception as xcp:
                    ui.notify(f"Alias changes were saved locally, but authority sync failed: {xcp}", type="warning")
                    drafts_by_user[target_user_id] = _build_alias_draft(target_user_id)
                    refresh_alias_body()
                    return
                drafts_by_user[target_user_id] = _build_alias_draft(target_user_id)
                ui.notify("Saved alias changes." if changed_fields else "Alias values are unchanged.", type="positive")
                refresh_alias_body()

            def _handle_target_user_change(event: ModWebValueContainer) -> None:
                if not can_switch_user:
                    return
                raw_value = str(_value_as_object(event) or "").strip()
                if not raw_value.isdigit():
                    ui.notify("Selected user is invalid.", type="negative")
                    return
                next_user_id = int(raw_value)
                if next_user_id == state["target_user_id"]:
                    return
                _capture_alias_draft()
                state["target_user_id"] = next_user_id
                refresh_alias_body()

            with ui.column().classes("w-full"):
                with ui.card().classes("mod-card w-full max-w-5xl self-center"):
                    with ui.column().classes("w-full gap-4 mod-app-details-layout"):
                        with ui.column().classes("gap-1"):
                            ui.label("Identity & Aliases").classes("text-xl font-black mod-title-small")
                            ui.label("Manage display names, aliases, game handles, and linked accounts.").classes(
                                "mod-subtitle text-sm"
                            )
                        target_select = (
                            ui.select(
                                self._alias_target_options(name_cache=name_cache, viewer=user),
                                value=str(state["target_user_id"]),
                                label="Editing User",
                                on_change=_handle_target_user_change,
                            )
                            .props("filled square dense hide-bottom-space color=accent options-dark")
                            .classes("mod-app-details-field w-full")
                        )
                        if not can_switch_user:
                            target_select.disable()
                        if can_switch_user:
                            with ui.column().classes("mod-app-details-section gap-3"):
                                ui.label("Manual User").classes("mod-stat-label")
                                manual_user_id_input = (
                                    ui.input("Discord User ID")
                                    .props("filled square dense hide-bottom-space color=accent")
                                    .classes("mod-app-details-field w-full")
                                )
                                manual_display_name_input = (
                                    ui.input("Manual Display Name (optional)")
                                    .props("filled square dense hide-bottom-space color=accent")
                                    .classes("mod-app-details-field w-full")
                                )

                                async def _open_manual_user() -> None:
                                    target_user_id: int | None = None
                                    try:
                                        target_user_id = self._parse_manual_alias_target_user_id(
                                            _value_as_object(manual_user_id_input)
                                        )
                                        if target_user_id in name_cache.by_id:
                                            raise ValueError("User is already cached; select them from the user list instead.")
                                        display_name = _value_as_text(manual_display_name_input) or None
                                        changed = name_cache.upsert_manual_user(
                                            target_user_id,
                                            display_name=display_name,
                                        )
                                        if changed:
                                            await self._sync_name_cache_with_authority_if_remote_async(
                                                name_cache=name_cache
                                            )
                                    except ValueError as xcp:
                                        ui.notify(str(xcp), type="negative")
                                        return
                                    except Exception as xcp:
                                        ui.notify(
                                            f"Manual user was saved locally, but authority sync failed: {xcp}",
                                            type="warning",
                                        )
                                    if target_user_id is None:
                                        raise RuntimeError("Manual user ID was not resolved.")
                                    _capture_alias_draft()
                                    state["target_user_id"] = target_user_id
                                    target_select.options = self._alias_target_options(name_cache=name_cache, viewer=user)
                                    _set_control_value(target_select, str(target_user_id))
                                    drafts_by_user[target_user_id] = _build_alias_draft(target_user_id)
                                    refresh_alias_body()

                                ui.button("Open Manual User", on_click=_open_manual_user).classes("mod-list-button secondary")

                        def _render_inline_alias_input(
                            *,
                            label: str,
                            value: str,
                            clear_tooltip: str,
                            clear_icon: str = "backspace",
                            control_key: str,
                        ) -> None:
                            with ui.element("div").classes(
                                "grid grid-cols-[minmax(0,1fr)_3rem] items-stretch gap-2 w-full min-w-0"
                            ):
                                control = (
                                    ui.input(label, value=value)
                                    .props("filled square dense hide-bottom-space color=accent")
                                    .classes("mod-app-details-field min-w-0")
                                )
                                form_controls[control_key] = control
                                def on_clear() -> None:
                                    _set_control_value(control, "")
                                    _capture_alias_draft()

                                clear_button = ui.button("", on_click=lambda _: on_clear()).props(
                                    f'icon={clear_icon} flat dense round aria-label="{clear_tooltip}"'
                                ).classes("mod-list-button secondary w-full min-w-0 px-2 py-2")
                                self._attach_text_tooltip(ui=ui, target=clear_button, text=clear_tooltip)

                        def _render_alias_body() -> None:
                            target_user_id = _target_user_id()
                            form_controls.clear()
                            app_alias_controls.clear()
                            target_names = name_cache.by_id.get(target_user_id, config.UserNames())
                            draft = _draft_for_user(target_user_id)
                            current_aliases = tuple(sorted(target_names.nicknames, key=str.casefold))
                            current_app_scopes = _current_app_scopes()
                            with ui.column().classes("w-full gap-4"):
                                with ui.column().classes("mod-app-details-section gap-3"):
                                    ui.label("Display Name").classes("mod-stat-label")
                                    _render_inline_alias_input(
                                        label="Display Name",
                                        value=draft.display_name,
                                        clear_tooltip="Clear display name",
                                        control_key="display_name",
                                    )

                                with ui.column().classes("mod-app-details-section gap-3"):
                                    ui.label("General Aliases").classes("mod-stat-label")
                                    def _add_general_alias() -> None:
                                        _capture_alias_draft()
                                        draft = _draft_for_user(target_user_id)
                                        value = draft.add_alias
                                        draft.add_alias = ""
                                        self._add_alias_general_name(
                                            ui=ui,
                                            name_cache=name_cache,
                                            user_id=target_user_id,
                                            value=value,
                                            refresh=refresh_alias_body,
                                        )

                                    with ui.element("div").classes(
                                        "grid grid-cols-[minmax(0,1fr)_3rem_3rem] items-stretch gap-2 w-full min-w-0"
                                    ):
                                        add_alias_control = (
                                            ui.input("Add General Alias", value=draft.add_alias)
                                            .props("filled square dense hide-bottom-space color=accent")
                                            .classes("mod-app-details-field min-w-0")
                                        )
                                        form_controls["add_alias"] = add_alias_control
                                        reset_alias_button = ui.button(
                                            "",
                                            on_click=lambda _:
                                            (
                                                _set_control_value(add_alias_control, ""),
                                                _capture_alias_draft(),
                                            ),
                                        ).props('icon=restart_alt flat dense round aria-label="Reset alias input"').classes(
                                            "mod-list-button secondary w-full min-w-0 px-2 py-2"
                                        )
                                        add_alias_button = ui.button(
                                            "",
                                            on_click=lambda _: _add_general_alias(),
                                        ).props('icon=add flat dense round aria-label="Add general alias"').classes(
                                            "mod-list-button w-full min-w-0 px-2 py-2"
                                        )
                                        self._attach_text_tooltip(
                                            ui=ui,
                                            target=reset_alias_button,
                                            text="Reset alias input",
                                        )
                                        self._attach_text_tooltip(
                                            ui=ui,
                                            target=add_alias_button,
                                            text="Add general alias",
                                        )
                                    if current_aliases:
                                        for alias in current_aliases:
                                            with ui.row().classes("w-full items-center justify-between gap-2"):
                                                ui.label(alias).classes("mod-subtitle text-sm break-all")
                                                remove_button = ui.button(
                                                    "",
                                                    on_click=lambda _, alias=alias:
                                                    (
                                                        _capture_alias_draft(),
                                                        self._remove_alias_general_name(
                                                            ui=ui,
                                                            name_cache=name_cache,
                                                            user_id=target_user_id,
                                                            alias=alias,
                                                            refresh=refresh_alias_body,
                                                        ),
                                                    ),
                                                ).props('icon=delete flat dense round aria-label="Remove alias"').classes(
                                                    "mod-list-button secondary shrink-0 px-2 py-2"
                                                )
                                                self._attach_text_tooltip(ui=ui, target=remove_button, text="Remove alias")
                                    else:
                                        ui.label("No general aliases set.").classes("mod-subtitle text-xs")

                                with ui.column().classes("mod-app-details-section gap-3"):
                                    ui.label("App Aliases").classes("mod-stat-label")
                                    if current_app_scopes:
                                        for scope in current_app_scopes:
                                            _render_inline_alias_input(
                                                label=f"{scope.title()} Alias",
                                                value=draft.app_aliases.get(scope, ""),
                                                clear_tooltip=f"Clear {scope.title()} alias",
                                                control_key=f"app_alias:{scope}",
                                            )
                                            app_alias_controls[scope] = form_controls[f"app_alias:{scope}"]
                                    else:
                                        ui.label("No app scopes are available on this node.").classes(
                                            "mod-subtitle text-xs"
                                        )

                                with ui.column().classes("mod-app-details-section gap-3"):
                                    ui.label("Linked Accounts").classes("mod-stat-label")
                                    _render_inline_alias_input(
                                        label="Steam ID",
                                        value=draft.steam_id,
                                        clear_tooltip="Clear Steam ID",
                                        control_key="steam_id",
                                    )
                                    _render_inline_alias_input(
                                        label="Minecraft UUID",
                                        value=draft.minecraft_uuid,
                                        clear_tooltip="Clear Minecraft UUID",
                                        control_key="minecraft_uuid",
                                    )

                        refreshable = getattr(ui, "refreshable", None)
                        if callable(refreshable):
                            render_alias_body = ui.refreshable(_render_alias_body)
                            refresh_alias_body = render_alias_body.refresh
                        else:
                            render_alias_body = _render_alias_body
                            refresh_alias_body = _render_alias_body

                        render_alias_body()
                        with ui.row().classes("w-full justify-end gap-2 mod-app-details-actions"):
                            ui.button("Save", on_click=_save_alias_form).classes("mod-list-button")
                            ui.button(
                                "Back to Dashboard",
                                on_click=lambda: ui.navigate.to(self.index_path()),
                            ).classes("mod-list-button secondary")

        _render_alias_page()

    @staticmethod
    def _save_alias_display_override(
        *,
        ui: ModWebUi,
        name_cache: config.Name_Cache,
        user_id: int,
        value: str | None,
        refresh: Callable[[], None],
    ) -> None:
        try:
            changed = name_cache.set_display_override(user_id, value)
        except ValueError as xcp:
            ui.notify(str(xcp), type="negative")
            return
        ui.notify("Saved display name." if changed else "Display name is unchanged.", type="positive")
        refresh()

    @staticmethod
    def _add_alias_general_name(
        *,
        ui: ModWebUi,
        name_cache: config.Name_Cache,
        user_id: int,
        value: str,
        refresh: Callable[[], None],
    ) -> None:
        try:
            name_cache.add_name(user_id, value, False)
        except ValueError as xcp:
            ui.notify(str(xcp), type="negative")
            return
        ui.notify("Added general alias.", type="positive")
        refresh()

    @staticmethod
    def _remove_alias_general_name(
        *,
        ui: ModWebUi,
        name_cache: config.Name_Cache,
        user_id: int,
        alias: str,
        refresh: Callable[[], None],
    ) -> None:
        name_cache.remove_name(user_id, alias)
        ui.notify(f"Removed `{alias}`.", type="positive")
        refresh()

    @staticmethod
    def _save_alias_game_name(
        *,
        ui: ModWebUi,
        name_cache: config.Name_Cache,
        user_id: int,
        scope: str,
        value: str,
        refresh: Callable[[], None],
    ) -> None:
        try:
            name_cache.set_game_alias(user_id, scope, value)
        except ValueError as xcp:
            ui.notify(str(xcp), type="negative")
            return
        ui.notify(f"Saved {scope.title()} alias.", type="positive")
        refresh()

    @staticmethod
    def _clear_alias_game_name(
        *,
        ui: ModWebUi,
        name_cache: config.Name_Cache,
        user_id: int,
        scope: str,
        refresh: Callable[[], None],
    ) -> None:
        name_cache.remove_game_alias(user_id, scope)
        ui.notify(f"Cleared {scope.title()} alias.", type="positive")
        refresh()

    @staticmethod
    def _save_alias_platform_id(
        *,
        ui: ModWebUi,
        name_cache: config.Name_Cache,
        user_id: int,
        platform: str,
        value: str | None,
        refresh: Callable[[], None],
    ) -> None:
        try:
            changed = name_cache.set_platform_id(user_id, platform, value)
        except ValueError as xcp:
            ui.notify(str(xcp), type="negative")
            return
        ui.notify(
            f"Saved {platform.title()} identity." if changed else f"{platform.title()} identity is unchanged.",
            type="positive",
        )
        refresh()

    @staticmethod
    def _save_alias_game_uuid(
        *,
        ui: ModWebUi,
        name_cache: config.Name_Cache,
        user_id: int,
        scope: str,
        value: str | None,
        refresh: Callable[[], None],
    ) -> None:
        try:
            changed = name_cache.set_game_uuid(user_id, scope, value)
        except ValueError as xcp:
            ui.notify(str(xcp), type="negative")
            return
        ui.notify(
            f"Saved {scope.title()} UUID." if changed else f"{scope.title()} UUID is unchanged.",
            type="positive",
        )
        refresh()

