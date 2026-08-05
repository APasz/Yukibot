"""User utility-menu UI helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

from nicegui.element import Element

import config
from mod_web_auth import ModWebUser

from .nicegui_protocols import ModWebUi
from .status_support import _USER_HEADER_ICON_BUTTON_CLASSES, ModWebStatusFeatureSupport
from .types import ModWebNotificationTrayItemKind
from .ui_helpers import ModWebUiHelpersMixin
from .user_plate import user_plate_action_spec
from .user_settings import ModWebUserPlateAction


type _UtilityAction = Callable[[], None]


@dataclass(frozen=True, slots=True)
class _UtilityActionSpec:
    """A user action shared by the header plate and its utilities menu."""

    label: str
    icon: str | None
    action: _UtilityAction
    plate_action: ModWebUserPlateAction | None = None


class _UtilityPanelBuilder(Protocol):
    def _build_user_settings_panel(self, *, ui: ModWebUi, user: ModWebUser) -> _UtilityAction: ...

    def _build_standard_drinks_panel(self, *, ui: ModWebUi, user: ModWebUser) -> _UtilityAction: ...

    def _build_currency_converter_panel(self, *, ui: ModWebUi, user: ModWebUser) -> _UtilityAction: ...

    def _build_time_formatter_panel(self, *, ui: ModWebUi, user: ModWebUser) -> _UtilityAction: ...

    def _build_unit_converter_panel(self, *, ui: ModWebUi) -> _UtilityAction: ...


class ModWebStatusUtilityLauncherMixin(ModWebStatusFeatureSupport):
    def _render_user_utility_launcher(
        self,
        *,
        ui: ModWebUi,
        user: ModWebUser,
        include_mirrors: bool = False,
    ) -> None:
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

        def _plate_action(
            plate_action: ModWebUserPlateAction,
            action: _UtilityAction,
        ) -> _UtilityActionSpec:
            presentation = user_plate_action_spec(plate_action)
            return _UtilityActionSpec(
                label=presentation.label,
                icon=presentation.icon,
                action=action,
                plate_action=plate_action,
            )

        action_specs: list[_UtilityActionSpec] = []
        if config.INDEV:
            action_specs.extend(
                (
                    _UtilityActionSpec(
                        label="Sim Upload",
                        icon=None,
                        action=lambda: _simulate(ModWebNotificationTrayItemKind.UPLOAD),
                    ),
                    _UtilityActionSpec(
                        label="Sim Download",
                        icon=None,
                        action=lambda: _simulate(ModWebNotificationTrayItemKind.DOWNLOAD),
                    ),
                    _UtilityActionSpec(label="Clear Transfers", icon=None, action=_clear_transfers),
                )
            )
        if include_mirrors:
            action_specs.append(
                _plate_action(ModWebUserPlateAction.MIRRORS, lambda: ui.navigate.to("/mod-web/mirrors"))
            )
        action_specs.append(_plate_action(ModWebUserPlateAction.SETTINGS, open_user_settings))
        action_specs.append(_plate_action(ModWebUserPlateAction.STANDARD_DRINKS, open_standard_drinks))
        action_specs.append(_plate_action(ModWebUserPlateAction.CURRENCY, open_currency_converter))
        action_specs.append(_plate_action(ModWebUserPlateAction.DISCORD_TIME, open_time_formatter))
        action_specs.append(_plate_action(ModWebUserPlateAction.UNIT_CONVERTER, open_unit_converter))
        action_specs.append(_plate_action(ModWebUserPlateAction.ALIASES, _open_alias_page))
        action_specs.append(_UtilityActionSpec(label="About", icon="info", action=_open_about_page))
        action_specs.append(
            _plate_action(ModWebUserPlateAction.LOG_OUT, lambda: ui.navigate.to("/auth/logout"))
        )

        def _action_handler(action: _UtilityAction) -> Callable[[object], None]:
            def _on_click(_: object) -> None:
                action()

            return _on_click

        selected_plate_actions = self._backend.user_settings_for(user_id=user.discord_id).user_plate.visible_actions
        for action_spec in action_specs:
            if action_spec.plate_action not in selected_plate_actions:
                continue
            if action_spec.icon is None:
                continue
            plate_button = ui.button("", on_click=_action_handler(action_spec.action)).props(
                f"icon={action_spec.icon} flat aria-label={action_spec.label}"
            ).classes(f"{_USER_HEADER_ICON_BUTTON_CLASSES} mod-user-plate-button")
            ModWebUiHelpersMixin._attach_badge_tooltip(
                ui=ui,
                target=plate_button,
                text=action_spec.label,
            )

        menu_factory = getattr(ui, "menu", None)
        if callable(menu_factory):
            create_menu = cast(Callable[[], Element], menu_factory)

            utility_button = (
                ui.button("")
                .props("icon=menu flat aria-label=Utilities")
                .classes(f"{_USER_HEADER_ICON_BUTTON_CLASSES} mod-user-menu-button")
            )
            ModWebUiHelpersMixin._attach_badge_tooltip(ui=ui, target=utility_button, text="Utilities")
            with utility_button:
                with create_menu().classes("mod-chat-entry-menu min-w-[13rem]"):
                    for action_spec in action_specs:
                        item_classes = "mod-chat-entry-menu-item"
                        if action_spec.icon is not None:
                            item_classes += " mod-user-utility-menu-item"
                        menu_item = ui.menu_item(
                            action_spec.label,
                            on_click=_action_handler(action_spec.action),
                        ).classes(item_classes)
                        if action_spec.icon is not None:
                            with menu_item:
                                with ui.item_section().props("avatar"):
                                    ui.icon(action_spec.icon).classes("text-base")
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
                    for action_spec in action_specs:
                        button = ui.button(
                            action_spec.label,
                            on_click=_dialog_action_handler(action_spec.action),
                        ).classes(
                            "mod-list-button secondary w-full"
                        )
                        if action_spec.icon is not None:
                            button.props(f"icon={action_spec.icon}")

        utility_button = ui.button("", on_click=utility_dialog.open).props("icon=menu flat aria-label=Utilities").classes(
            f"{_USER_HEADER_ICON_BUTTON_CLASSES} mod-user-menu-button"
        )
        ModWebUiHelpersMixin._attach_badge_tooltip(ui=ui, target=utility_button, text="Utilities")
