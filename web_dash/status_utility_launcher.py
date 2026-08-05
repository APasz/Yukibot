"""User utility-menu UI helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast

from nicegui.element import Element

import config
from mod_web_auth import ModWebUser

from .nicegui_protocols import ModWebUi
from .status_support import _USER_HEADER_ICON_BUTTON_CLASSES, ModWebStatusFeatureSupport
from .types import ModWebNotificationTrayItemKind


type _UtilityAction = Callable[[], None]


class _UtilityPanelBuilder(Protocol):
    def _build_user_settings_panel(self, *, ui: ModWebUi, user: ModWebUser) -> _UtilityAction: ...

    def _build_standard_drinks_panel(self, *, ui: ModWebUi, user: ModWebUser) -> _UtilityAction: ...

    def _build_currency_converter_panel(self, *, ui: ModWebUi, user: ModWebUser) -> _UtilityAction: ...

    def _build_time_formatter_panel(self, *, ui: ModWebUi, user: ModWebUser) -> _UtilityAction: ...

    def _build_unit_converter_panel(self, *, ui: ModWebUi) -> _UtilityAction: ...


class ModWebStatusUtilityLauncherMixin(ModWebStatusFeatureSupport):
    def _render_user_utility_launcher(self, *, ui: ModWebUi, user: ModWebUser) -> None:
        panels: _UtilityPanelBuilder = cast(_UtilityPanelBuilder, cast(object, self))
        open_user_settings: _UtilityAction = panels._build_user_settings_panel(ui=ui, user=user)
        open_standard_drinks: _UtilityAction = panels._build_standard_drinks_panel(ui=ui, user=user)
        open_currency_converter: _UtilityAction = panels._build_currency_converter_panel(ui=ui, user=user)
        open_time_formatter: _UtilityAction = panels._build_time_formatter_panel(ui=ui, user=user)
        open_unit_converter: _UtilityAction = panels._build_unit_converter_panel(ui=ui)

        def _open_alias_page() -> None:
            ui.navigate.to("/aliases")

        def _open_about_page() -> None:
            ui.navigate.to("/auth/about")

        def _simulate(kind: ModWebNotificationTrayItemKind) -> None:
            current_count: int = len(self._backend.user_transfer_items(user_id=user.discord_id))
            filename = (
                f"sim-upload-{current_count + 1:02d}.jar"
                if kind is ModWebNotificationTrayItemKind.UPLOAD
                else f"sim-download-{current_count + 1:02d}.zip"
            )
            detail_text = (
                "Simulated upload transfer."
                if kind is ModWebNotificationTrayItemKind.UPLOAD
                else "Simulated download transfer."
            )
            try:
                self._backend.start_simulated_transfer(
                    user_id=user.discord_id,
                    kind=kind,
                    filename=filename,
                    detail_text=detail_text,
                    duration_seconds=6.0,
                    node_color_hex=self._primary_guild_bot_role_color_hex(),
                    app_color_hex=None,
                )
            except RuntimeError as xcp:
                ui.notify(str(xcp), type="warning")

        def _clear_transfers() -> None:
            self._backend.clear_user_transfers(user_id=user.discord_id)

        action_specs: list[tuple[str, _UtilityAction]] = []
        if config.INDEV:
            action_specs.extend(
                (
                    ("Sim Upload", lambda: _simulate(ModWebNotificationTrayItemKind.UPLOAD)),
                    ("Sim Download", lambda: _simulate(ModWebNotificationTrayItemKind.DOWNLOAD)),
                    ("Clear Transfers", _clear_transfers),
                )
            )
        action_specs.append(("Settings", open_user_settings))
        action_specs.append(("Standard drinks", open_standard_drinks))
        action_specs.append(("Currency", open_currency_converter))
        action_specs.append(("Discord Time", open_time_formatter))
        action_specs.append(("Unit converter", open_unit_converter))
        action_specs.append(("Aliases", _open_alias_page))
        action_specs.append(("About", _open_about_page))
        action_specs.append(("Log out", lambda: ui.navigate.to("/auth/logout")))

        menu_factory = getattr(ui, "menu", None)
        if callable(menu_factory):
            create_menu = cast(Callable[[], Element], menu_factory)

            def _menu_action_handler(action: _UtilityAction) -> Callable[[object], None]:
                def _on_click(_: object) -> None:
                    action()

                return _on_click

            with (
                ui.button("")
                .props("icon=menu flat aria-label=Utilities")
                .classes(f"{_USER_HEADER_ICON_BUTTON_CLASSES} mod-user-menu-button")
            ):
                with create_menu().classes("mod-chat-entry-menu min-w-[12rem]"):
                    for label, action in action_specs:
                        ui.menu_item(label, on_click=_menu_action_handler(action)).classes("mod-chat-entry-menu-item")
            return

        with ui.dialog() as utility_dialog:

            def _dialog_action_handler(action: _UtilityAction) -> Callable[[object], None]:
                def _on_click(_: object) -> None:
                    action()
                    utility_dialog.close()

                return _on_click

            with ui.card().classes("mod-card mod-dialog-card"):
                with ui.column().classes("w-full gap-2 p-4"):
                    ui.label("Tray Tools").classes("text-lg font-black mod-title-small")
                    for label, action in action_specs:
                        ui.button(label, on_click=_dialog_action_handler(action)).classes(
                            "mod-list-button secondary w-full"
                        )

        ui.button("", on_click=utility_dialog.open).props("icon=menu flat aria-label=Utilities").classes(
            f"{_USER_HEADER_ICON_BUTTON_CLASSES} mod-user-menu-button"
        )
