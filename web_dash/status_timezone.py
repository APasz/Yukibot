"""Reusable timezone controls for status UI panels."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from nicegui.elements.input import Input

from _utils import Utilities

from .nicegui_protocols import ModWebUi, _value_as_text
from .status_support import ModWebStatusFeatureSupport


@dataclass(slots=True)
class _TimezonePicker:
    input: Input
    set_timezone: Callable[[str], None]


class ModWebStatusTimezoneMixin(ModWebStatusFeatureSupport):
    @staticmethod
    def _build_timezone_picker(
        *,
        ui: ModWebUi,
        value: str,
        field_classes: str,
        on_change: Callable[[str], None] | None = None,
    ) -> _TimezonePicker:
        search_query = ""
        menu_open = False
        with ui.element("div").classes("mod-timezone-picker w-full min-w-0"):
            timezone_input = (
                ui.input("Timezone", value=value)
                .props("filled square dense stack-label hide-bottom-space inputmode=text")
                .classes(field_classes)
            )
            with timezone_input.add_slot("append"):
                menu_button = ui.button("").props("icon=arrow_drop_down flat dense aria-label=Timezone options")
            options_container = ui.column().classes(
                "mod-setting-menu mod-timezone-menu mod-timezone-options w-full gap-1 p-1"
            )
            options_container.set_visibility(False)

        def _render_options() -> None:
            timezone_options = Utilities.timezone_selection_options(search_query)

            def _option_handler(timezone_name: str) -> Callable[[object], None]:
                def _on_click(_: object) -> None:
                    _select_timezone(timezone_name)

                return _on_click

            options_container.clear()
            with options_container:
                if not timezone_options:
                    ui.label("No matching timezones.").classes("mod-stat-label px-2 py-1")
                for option in timezone_options:
                    with (
                        ui.button(
                            "",
                            on_click=_option_handler(option.value),
                            color=None,
                        )
                        .props("flat no-caps")
                        .classes("mod-timezone-option w-full")
                    ):
                        with ui.row().classes("mod-timezone-option-summary w-full items-baseline no-wrap"):
                            ui.label(option.timezone_code).classes("mod-timezone-option-code")
                            ui.label(option.offset_text).classes("mod-timezone-option-offset")
                        if option.location_text is not None:
                            ui.label(option.location_text).classes("mod-timezone-option-location")

        def _set_menu_open(is_open: bool) -> None:
            nonlocal menu_open
            menu_open = is_open
            options_container.set_visibility(is_open)

        def _set_timezone(timezone_name: str) -> None:
            nonlocal search_query
            search_query = ""
            timezone_input.set_value(timezone_name)
            _render_options()

        def _select_timezone(timezone_name: str) -> None:
            _set_timezone(timezone_name)
            _set_menu_open(False)
            if on_change is not None:
                on_change(timezone_name)

        def _handle_input(_: object | None = None) -> None:
            nonlocal search_query
            search_query = _value_as_text(timezone_input)
            _render_options()
            _set_menu_open(True)
            if on_change is not None:
                on_change(search_query)

        def _toggle_menu(_: object | None = None) -> None:
            _set_menu_open(not menu_open)

        timezone_input.on("update:value", _handle_input)
        menu_button.on("click", _toggle_menu)
        _render_options()
        return _TimezonePicker(input=timezone_input, set_timezone=_set_timezone)
