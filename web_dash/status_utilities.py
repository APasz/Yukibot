"""StatusUtilities UI helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nicegui.element import Element  # noqa: F401 - used by a quoted cast annotation

# ruff: noqa: F403, F405
from .status_support import *


class ModWebStatusUtilitiesMixin(ModWebStatusFeatureSupport):
    def _render_user_utility_launcher(self, *, ui: ModWebUi, user: ModWebUser) -> None:
        open_user_settings = self._build_user_settings_panel(ui=ui, user=user)
        open_standard_drinks = self._build_standard_drinks_panel(ui=ui, user=user)
        open_currency_converter = self._build_currency_converter_panel(ui=ui, user=user)
        open_time_formatter = self._build_time_formatter_panel(ui=ui, user=user)

        def _open_alias_page() -> None:
            ui.navigate.to("/aliases")

        def _open_about_page() -> None:
            ui.navigate.to("/auth/about")

        open_discord_settings = (
            self._build_discord_settings_panel(ui=ui, user=user) if self._user_can_manage_discord_settings(user) else None
        )

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

        action_specs: list[tuple[str, Callable[[], object]]] = []
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
        action_specs.append(("Aliases", _open_alias_page))
        action_specs.append(("About", _open_about_page))
        if open_discord_settings is not None:
            action_specs.append(("Discord", open_discord_settings))
        action_specs.append(("Log out", lambda: ui.navigate.to("/auth/logout")))

        menu_factory = getattr(ui, "menu", None)
        if callable(menu_factory):
            create_menu = cast("Callable[[], Element]", menu_factory)
            with ui.button("").props("icon=menu flat aria-label=Utilities").classes(
                f"{_USER_HEADER_ICON_BUTTON_CLASSES} mod-user-menu-button"
            ):
                with create_menu().classes("mod-chat-entry-menu min-w-[12rem]"):
                    for label, action in action_specs:
                        ui.menu_item(label, on_click=lambda _, action=action: action()).classes("mod-chat-entry-menu-item")
            return

        with ui.dialog() as utility_dialog:
            with ui.card().classes("mod-card mod-dialog-card"):
                with ui.column().classes("w-full gap-2 p-4"):
                    ui.label("Tray Tools").classes("text-lg font-black mod-title-small")
                    for label, action in action_specs:
                        ui.button(label, on_click=lambda _, action=action: (action(), utility_dialog.close())).classes(
                            "mod-list-button secondary w-full"
                        )

        ui.button("", on_click=utility_dialog.open).props("icon=menu flat aria-label=Utilities").classes(
            f"{_USER_HEADER_ICON_BUTTON_CLASSES} mod-user-menu-button"
        )

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
            options_container.clear()
            with options_container:
                if not timezone_options:
                    ui.label("No matching timezones.").classes("mod-stat-label px-2 py-1")
                for option in timezone_options:
                    with (
                        ui.button(
                            "",
                            on_click=lambda _, timezone_name=option.value: _select_timezone(timezone_name),
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

    def _build_user_settings_panel(self, *, ui: ModWebUi, user: ModWebUser) -> Callable[[], None]:
        def _show_user_settings_panel() -> None:
            current_settings = self._backend.user_settings_for(user_id=user.discord_id)
            current_colors = self._resolved_user_appearance_colors(current_settings.appearance)
            country_options: dict[str, str] = {
                country.value: country.display_name for country in config.supported_conversion_countries()
            }
            with ui.dialog() as settings_dialog:
                with ui.card().classes("mod-card mod-dialog-card mod-app-details-dialog-card"):
                    with ui.column().classes("w-full gap-4 mod-app-details-layout"):
                        with ui.column().classes("gap-1"):
                            ui.label(f"{self._web_display_name(user)} Settings").classes(
                                "text-xl font-black mod-title-small"
                            )
                        with ui.column().classes("mod-app-details-section mod-user-appearance-section gap-2"):
                            ui.label("Appearance").classes("mod-stat-label")
                            color_inputs: dict[_UserAppearanceColorKey, ModWebValueContainer] = {}
                            with ui.element("div").classes("mod-user-appearance-grid"):
                                for spec in _USER_APPEARANCE_COLOR_SPECS:
                                    color_inputs[spec.key] = (
                                        ui.input(
                                            spec.label,
                                            value=current_colors[spec.key],
                                        )
                                        .props("filled square dense hide-bottom-space color=accent type=color")
                                        .classes(
                                            "mod-app-details-field mod-user-accent-input w-full min-w-0"
                                        )
                                    )

                            def _capture_color_values() -> dict[_UserAppearanceColorKey, str]:
                                return {
                                    spec.key: self._normalized_user_appearance_color_hex(_value_as_text(color_inputs[spec.key]))
                                    for spec in _USER_APPEARANCE_COLOR_SPECS
                                }

                            def _apply_color_values_to_controls(colors: dict[_UserAppearanceColorKey, str]) -> None:
                                for spec in _USER_APPEARANCE_COLOR_SPECS:
                                    set_value = getattr(color_inputs[spec.key], "set_value", None)
                                    if callable(set_value):
                                        set_value(colors[spec.key])

                            tooltip_above_on_touch_input = ui.checkbox(
                                "Tooltip above on touch device",
                                value=current_settings.appearance.tooltip_above_on_touch_device,
                            ).props("dense color=accent").classes("mod-app-details-field")

                            def _capture_tooltip_above_on_touch_device() -> bool:
                                value = tooltip_above_on_touch_input.value
                                if not isinstance(value, bool):
                                    raise ValueError("Tooltip placement must be enabled or disabled.")
                                return value

                            def _apply_tooltip_placement_to_control(settings: ModWebUserSettings) -> None:
                                tooltip_above_on_touch_input.set_value(
                                    settings.appearance.tooltip_above_on_touch_device
                                )

                        with ui.column().classes("mod-app-details-section gap-2"):
                            ui.label("Location").classes("mod-stat-label")
                            country_input = (
                                ui.select(
                                    country_options,
                                    value=current_settings.country.value
                                    if current_settings.country is not None
                                    else None,
                                    label="Country",
                                )
                                .props(
                                    "filled square dense clearable hide-bottom-space color=accent options-dark "
                                    "popup-content-class=mod-setting-menu"
                                )
                                .classes("mod-app-details-field w-full")
                            )

                            def _capture_country() -> config.Country | None:
                                raw_country = _value_as_text(country_input).strip()
                                if not raw_country:
                                    return None
                                try:
                                    country = config.Country(raw_country)
                                except ValueError as xcp:
                                    raise ValueError("Choose a supported country.") from xcp
                                if country not in config.supported_conversion_countries():
                                    raise ValueError("Choose a supported country.")
                                return country

                            def _apply_country_to_control(country: config.Country | None) -> None:
                                set_value = getattr(country_input, "set_value", None)
                                if callable(set_value):
                                    set_value(country.value if country is not None else None)

                        with ui.column().classes("mod-app-details-section gap-2"):
                            ui.label("Timezone & format").classes("mod-stat-label")
                            with ui.element("div").classes("w-full grid grid-cols-1 md:grid-cols-2 gap-2"):
                                timezone_picker = self._build_timezone_picker(
                                    ui=ui,
                                    value=current_settings.timestamp.timezone_name,
                                    field_classes="mod-app-details-field mod-config-input w-full min-w-0",
                                )
                                time_format_input = (
                                    ui.select(
                                        _TIME_FORMAT_OPTIONS,
                                        value="24" if current_settings.web_chat.use_24_hour_time else "12",
                                        label="Time format",
                                    )
                                    .props(
                                        "filled square dense stack-label hide-bottom-space color=accent options-dark "
                                        "popup-content-class=mod-setting-menu"
                                    )
                                    .classes("mod-app-details-field mod-config-select w-full min-w-0")
                                )

                            def _capture_use_24_hour_time() -> bool:
                                time_format = _value_as_text(time_format_input)
                                if time_format == "24":
                                    return True
                                if time_format == "12":
                                    return False
                                raise ValueError("Choose either 24-hour or 12-hour time.")

                            def _apply_time_preferences_to_controls(settings: ModWebUserSettings) -> None:
                                timezone_picker.set_timezone(settings.timestamp.timezone_name)
                                time_format_input.set_value("24" if settings.web_chat.use_24_hour_time else "12")

                            def _save_appearance_colors(_: object | None = None) -> None:
                                nonlocal current_settings
                                try:
                                    next_colors = _capture_color_values()
                                    next_settings = ModWebUserSettings(
                                        appearance=self._user_appearance_with_colors(
                                            appearance=current_settings.appearance,
                                            colors_by_key=next_colors,
                                            tooltip_above_on_touch_device=_capture_tooltip_above_on_touch_device(),
                                        ),
                                        web_chat=ModWebChatSettings(
                                            use_24_hour_time=_capture_use_24_hour_time()
                                        ),
                                        timestamp=ModWebTimestampSettings(
                                            timezone_name=_value_as_text(timezone_picker.input),
                                            format_template=current_settings.timestamp.format_template,
                                            rounding_unit=current_settings.timestamp.rounding_unit,
                                        ),
                                        country=_capture_country(),
                                    )
                                    changed = self._backend.save_user_settings(
                                        user_id=user.discord_id,
                                        settings=next_settings,
                                    )
                                except ValueError as xcp:
                                    ui.notify(str(xcp), type="negative")
                                    return
                                except Exception as xcp:
                                    log.warning("User settings update failed: user=%s error=%s", user.discord_id, xcp)
                                    ui.notify(f"Settings update failed: {xcp}", type="negative")
                                    return

                                current_settings = next_settings
                                css_variables = self._user_appearance_css_variables(next_settings.appearance)
                                try:
                                    ui.run_javascript(
                                        self._user_appearance_javascript(css_variables)
                                        + self._user_tooltip_placement_javascript(
                                            next_settings.appearance.tooltip_above_on_touch_device
                                        ),
                                        timeout=0.5,
                                    )
                                except Exception:
                                    pass
                                _apply_color_values_to_controls(
                                    self._resolved_user_appearance_colors(next_settings.appearance)
                                )
                                _apply_country_to_control(next_settings.country)
                                _apply_time_preferences_to_controls(next_settings)
                                _apply_tooltip_placement_to_control(next_settings)
                                ui.notify(
                                    "Saved settings." if changed else "Settings are unchanged.",
                                    type="positive",
                                )

                            def _reset_appearance_colors(_: object | None = None) -> None:
                                nonlocal current_settings
                                try:
                                    reset_colors: dict[_UserAppearanceColorKey, str | None] = {
                                        spec.key: None for spec in _USER_APPEARANCE_COLOR_SPECS
                                    }
                                    next_settings = ModWebUserSettings(
                                        appearance=self._user_appearance_with_colors(
                                            appearance=current_settings.appearance,
                                            colors_by_key=reset_colors,
                                            tooltip_above_on_touch_device=True,
                                        ),
                                        web_chat=ModWebChatSettings(use_24_hour_time=True),
                                        timestamp=ModWebTimestampSettings(
                                            timezone_name="UTC",
                                            format_template=current_settings.timestamp.format_template,
                                            rounding_unit=current_settings.timestamp.rounding_unit,
                                        ),
                                        country=None,
                                    )
                                    changed = self._backend.save_user_settings(
                                        user_id=user.discord_id,
                                        settings=next_settings,
                                    )
                                except Exception as xcp:
                                    log.warning("User settings reset failed: user=%s error=%s", user.discord_id, xcp)
                                    ui.notify(f"Settings reset failed: {xcp}", type="negative")
                                    return

                                current_settings = next_settings
                                _apply_color_values_to_controls(self._resolved_user_appearance_colors(next_settings.appearance))
                                _apply_country_to_control(next_settings.country)
                                _apply_time_preferences_to_controls(next_settings)
                                _apply_tooltip_placement_to_control(next_settings)
                                try:
                                    ui.run_javascript(
                                        self._user_appearance_javascript(None)
                                        + self._user_tooltip_placement_javascript(True),
                                        timeout=0.5,
                                    )
                                except Exception:
                                    pass
                                ui.notify(
                                    "Reset settings." if changed else "Settings are already default.",
                                    type="positive",
                                )
                        with ui.row().classes("w-full justify-end mod-app-details-actions"):
                            ui.button("Reset", on_click=_reset_appearance_colors).classes("mod-list-button secondary")
                            ui.button("Save", on_click=_save_appearance_colors).classes("mod-list-button")
                            ui.button("Close", on_click=settings_dialog.close).classes("mod-list-button secondary")
                settings_dialog.open()

        return _show_user_settings_panel

    def _build_standard_drinks_panel(self, *, ui: ModWebUi, user: ModWebUser) -> Callable[[], None]:
        unit_options: dict[str, str] = {
            unit: (
                f"{unit} · {standard_drink_definition(unit).country.display_name} · "
                f"{format_standard_drink_definition(standard_drink_definition(unit))}"
            )
            for unit in standard_drink_units(include_unavailable=False, exact_only=True)
        }
        table_columns: list[dict[str, str]] = [
            {"name": "unit", "label": "Definition", "field": "unit", "align": "left"},
            {"name": "basis", "label": "Basis", "field": "basis", "align": "left"},
            {"name": "grams", "label": "Alcohol / drink", "field": "grams", "align": "right"},
            {"name": "amount", "label": "Standard drinks", "field": "amount", "align": "right"},
        ]

        def _table_rows(*, pure_alcohol_grams: Decimal) -> list[dict[str, str]]:
            return [
                {
                    "unit": f"{equivalent.unit} · {equivalent.definition.country.display_name}",
                    "basis": equivalent.definition.kind.value.replace("_", " ").title(),
                    "grams": ""
                    if equivalent.amount is None
                    else format_standard_drink_definition(equivalent.definition),
                    "amount": "" if equivalent.amount is None else format_standard_drink_range(equivalent.amount),
                }
                for equivalent in standard_drink_equivalents(pure_alcohol_grams=pure_alcohol_grams)
            ]

        def _show_standard_drinks_panel() -> None:
            user_country = self._backend.user_settings_for(user_id=user.discord_id).country
            default_beverage = config.country_beverage_default(user_country)
            country_definition = next(
                (
                    definition
                    for definition in config.STANDARD_DRINK_DEFINITIONS
                    if definition.country is user_country and definition.has_exact_grams
                ),
                None,
            )
            default_unit = "AU" if country_definition is None else country_definition.unit
            initial_estimate = beverage_standard_drink_estimate(
                volume_millilitres=default_beverage.volume_millilitres,
                alcohol_by_volume_percent=default_beverage.alcohol_by_volume_percent,
            )
            initial_definition = standard_drink_definition(default_unit)
            initial_grams = initial_definition.minimum_grams
            if initial_grams is None:
                raise RuntimeError(
                    f"{initial_definition.country.display_name} must have a usable standard-drink definition."
                )
            initial_amount = initial_estimate.pure_alcohol_grams / initial_grams
            estimate_rows: list[_StandardDrinkEstimateInputRow] = []
            with ui.dialog() as standard_drinks_dialog:
                with ui.card().classes("mod-card mod-dialog-card mod-app-details-dialog-card"):
                    with ui.column().classes("w-full gap-4 mod-app-details-layout"):
                        with ui.column().classes("gap-1"):
                            ui.label("Standard drinks").classes("text-xl font-black mod-title-small")
                            ui.label(
                                "Convert a country definition or estimate the alcohol in one or more drinks. "
                                "Ranges and estimates are shown when a country has no single official measure."
                            ).classes("mod-stat-label")
                        with ui.column().classes("w-full gap-2"):
                            with (
                                ui.tabs()
                                .classes("mod-section-tabs mod-standard-drink-tabs")
                                .props(
                                    "dense inline-label active-color=primary indicator-color=primary"
                                ) as calculation_tabs
                            ):
                                convert_tab = ui.tab("Convert", icon="swap_horiz")
                                estimate_tab = ui.tab("Estimate", icon="calculate")
                            with ui.tab_panels(calculation_tabs, value=convert_tab, animated=False).classes(
                                "w-full bg-transparent"
                            ):
                                with ui.tab_panel(convert_tab).classes("w-full mod-app-details-section"):
                                    with ui.element("div").classes("w-full grid grid-cols-3 gap-2 items-end"):
                                        amount_input = (
                                            ui.input("Amount", value=format_standard_drink_number(initial_amount))
                                            .props("filled square dense hide-bottom-space inputmode=decimal")
                                            .classes("mod-app-details-field mod-config-input col-span-1 min-w-0")
                                        )
                                        from_unit_input = (
                                            ui.select(
                                                options=unit_options,
                                                value=default_unit,
                                                label="From definition",
                                            )
                                            .props(
                                                "filled square dense hide-bottom-space color=accent options-dark "
                                                "popup-content-class=mod-setting-menu"
                                            )
                                            .classes("mod-app-details-field mod-config-select col-span-2 min-w-0")
                                        )
                                with ui.tab_panel(estimate_tab).classes("w-full mod-app-details-section"):
                                    estimate_rows_container = ui.column().classes("w-full gap-2")
                                    estimate_actions = ui.row().classes("w-full")

                            def _update_table(*, pure_alcohol_grams: Decimal) -> None:
                                conversion_table.rows = _table_rows(pure_alcohol_grams=pure_alcohol_grams)
                                conversion_table.update()
                                calculation_summary_label.set_text(
                                    f"{format_standard_drink_number(pure_alcohol_grams)} g pure alcohol"
                                )

                            def _update_from_standard_drinks(*, amount_text: str) -> None:
                                try:
                                    conversion = standard_drink_conversion(
                                        amount=parse_standard_drink_expression(amount_text, field="Amount"),
                                        from_unit=_value_as_text(from_unit_input),
                                        to_unit=_value_as_text(from_unit_input),
                                    )
                                except (TypeError, ValueError) as xcp:
                                    calculation_summary_label.set_text(str(xcp))
                                    return
                                _update_table(pure_alcohol_grams=conversion.pure_alcohol_grams.exact_value)

                            def _update_from_estimate(
                                *,
                                changed_row: _StandardDrinkEstimateInputRow | None = None,
                                changed_field: Literal["volume", "abv"] | None = None,
                                event: object | None = None,
                            ) -> None:
                                try:
                                    total_pure_alcohol_grams = sum(
                                        (
                                            beverage_standard_drink_estimate(
                                                volume_millilitres=parse_standard_drink_expression(
                                                    _value_as_text(event)
                                                    if estimate_row is changed_row
                                                    and changed_field == "volume"
                                                    and event is not None
                                                    else _value_as_text(estimate_row.volume_input),
                                                    field=f"Volume {estimate_row_index + 1}",
                                                ),
                                                alcohol_by_volume_percent=parse_standard_drink_expression(
                                                    _value_as_text(event)
                                                    if estimate_row is changed_row
                                                    and changed_field == "abv"
                                                    and event is not None
                                                    else _value_as_text(estimate_row.abv_input),
                                                    field=f"Alcohol by volume {estimate_row_index + 1}",
                                                ),
                                            ).pure_alcohol_grams
                                            for estimate_row_index, estimate_row in enumerate(estimate_rows)
                                        ),
                                        Decimal(),
                                    )
                                    source_definition = standard_drink_definition(_value_as_text(from_unit_input))
                                    source_grams = source_definition.minimum_grams
                                    if source_grams is None:
                                        raise ValueError("Choose a nationally defined standard drink.")
                                    source_amount = total_pure_alcohol_grams / source_grams
                                except (TypeError, ValueError) as xcp:
                                    calculation_summary_label.set_text(str(xcp))
                                    return
                                amount_input.set_value(format_standard_drink_number(source_amount))
                                _update_table(pure_alcohol_grams=total_pure_alcohol_grams)

                            def _update_remove_buttons() -> None:
                                can_remove = len(estimate_rows) > 1
                                for estimate_row in estimate_rows:
                                    estimate_row.remove_button.set_enabled(can_remove)

                            def _remove_estimate_row(estimate_row: _StandardDrinkEstimateInputRow) -> None:
                                if len(estimate_rows) <= 1:
                                    return
                                estimate_rows.remove(estimate_row)
                                estimate_row.container.delete()
                                _update_remove_buttons()
                                _update_from_estimate()

                            def _add_estimate_row(*, volume_value: str, abv_value: str) -> None:
                                row_number = len(estimate_rows) + 1
                                estimate_row_reference: list[_StandardDrinkEstimateInputRow] = []
                                with estimate_rows_container:
                                    with ui.row() as estimate_row_container:
                                        estimate_row_container.classes("w-full items-end gap-2 flex-wrap")
                                        volume_input = (
                                            ui.input(f"Volume {row_number} (mL)", value=volume_value)
                                            .props("filled square dense hide-bottom-space inputmode=decimal")
                                            .classes(
                                                "mod-app-details-field mod-config-input grow basis-40 min-w-[10rem]"
                                            )
                                        )
                                        abv_input = (
                                            ui.input(f"Alcohol by volume {row_number} (%)", value=abv_value)
                                            .props("filled square dense hide-bottom-space inputmode=decimal")
                                            .classes(
                                                "mod-app-details-field mod-config-input grow basis-40 min-w-[10rem]"
                                            )
                                        )
                                        remove_button = ui.button(
                                            icon="delete_outline",
                                            on_click=lambda _, reference=estimate_row_reference: _remove_estimate_row(
                                                reference[0]
                                            ),
                                        ).classes("mod-list-button secondary")
                                estimate_row = _StandardDrinkEstimateInputRow(
                                    container=estimate_row_container,
                                    volume_input=volume_input,
                                    abv_input=abv_input,
                                    remove_button=remove_button,
                                )
                                estimate_row_reference.append(estimate_row)
                                estimate_rows.append(estimate_row)
                                volume_input.on(
                                    "update:model-value",
                                    lambda event: _update_from_estimate(
                                        changed_row=estimate_row,
                                        changed_field="volume",
                                        event=event,
                                    ),
                                )
                                abv_input.on(
                                    "update:model-value",
                                    lambda event: _update_from_estimate(
                                        changed_row=estimate_row,
                                        changed_field="abv",
                                        event=event,
                                    ),
                                )
                                _update_remove_buttons()

                            def _add_drink(_: object | None = None) -> None:
                                _add_estimate_row(volume_value="0", abv_value="0")

                            amount_input.on(
                                "update:model-value",
                                lambda event: _update_from_standard_drinks(
                                    amount_text=_value_as_text(event)
                                    if event is not None
                                    else _value_as_text(amount_input)
                                ),
                            )
                            from_unit_input.on(
                                "update:model-value",
                                lambda _: _update_from_standard_drinks(amount_text=_value_as_text(amount_input)),
                            )
                            _add_estimate_row(
                                volume_value=format_standard_drink_number(default_beverage.volume_millilitres),
                                abv_value=format_standard_drink_number(default_beverage.alcohol_by_volume_percent),
                            )
                            with estimate_actions:
                                ui.button("Add drink", icon="add", on_click=_add_drink).classes(
                                    "mod-list-button secondary"
                                )
                        with ui.column().classes("w-full gap-2"):
                            calculation_summary_label = ui.label(
                                f"{format_standard_drink_number(initial_estimate.pure_alcohol_grams)} g pure alcohol"
                            ).classes("mod-stat-label")
                            conversion_table = (
                                ui.table(
                                    columns=table_columns,
                                    rows=_table_rows(pure_alcohol_grams=initial_estimate.pure_alcohol_grams),
                                    row_key="unit",
                                )
                                .props("dense flat hide-bottom")
                                .classes("w-full mod-standard-drink-table")
                            )
                        with ui.row().classes("w-full justify-end mod-app-details-actions"):
                            ui.button("Close", on_click=standard_drinks_dialog.close).classes(
                                "mod-list-button secondary"
                            )
                standard_drinks_dialog.open()

        return _show_standard_drinks_panel

    def _build_time_formatter_panel(self, *, ui: ModWebUi, user: ModWebUser) -> Callable[[], None]:
        format_options: dict[str, str] = {
            template: f"{label} · {Utilities.DISCORD_TIMESTAMP_STYLE_REPRESENTATIONS[template[-2]]}"
            for label, template in Utilities.DISCORD_TIMESTAMP_FORMATS
        }

        def _show_time_formatter_panel() -> None:
            current_settings = self._backend.user_settings_for(user_id=user.discord_id)
            active_mode: _TimestampInputMode = "exact"
            with ui.dialog() as time_dialog:
                with ui.card().classes(
                    "mod-card mod-dialog-card mod-app-details-dialog-card mod-timestamp-dialog-card"
                ):
                    with ui.column().classes("w-full gap-4 mod-app-details-layout"):
                        with ui.column().classes("gap-1"):
                            ui.label("Discord timestamp").classes("text-xl font-black mod-title-small")
                            ui.label(
                                "Create a Discord timestamp from an exact instant or a duration from now."
                            ).classes("mod-stat-label")
                        with (
                            ui.tabs(value="exact", on_change=lambda event: _handle_mode_change(event))
                            .classes("mod-section-tabs")
                            .props(
                                "dense inline-label active-color=primary indicator-color=primary aria-label=Timestamp input mode"
                            )
                        ) as timestamp_mode_tabs:
                            exact_tab = ui.tab("exact", label="Exact time", icon="event")
                            relative_tab = ui.tab("relative", label="Relative time", icon="schedule")
                        with ui.tab_panels(timestamp_mode_tabs, value="exact", animated=False).classes(
                            "mod-timestamp-mode-panels w-full bg-transparent"
                        ):
                            with ui.tab_panel(exact_tab).classes("w-full mod-app-details-section"):
                                ui.label("Use an epoch, ISO, DMY, or zoned date and time.").classes("mod-stat-label")
                                with ui.element("div").classes("w-full grid grid-cols-1 md:grid-cols-2 gap-2"):
                                    exact_time_input = (
                                        ui.input("Date & time")
                                        .props(
                                            "filled square dense stack-label hide-bottom-space inputmode=text "
                                            "placeholder='e.g. 06/02/26 14:30 · epoch'"
                                        )
                                        .classes(
                                            "mod-app-details-field mod-config-input mod-timestamp-input w-full min-w-0"
                                        )
                                    )
                                    with exact_time_input.add_slot("append"):
                                        date_time_picker_button = ui.button("", color=None).props(
                                            "icon=event flat dense aria-label=Choose date and time"
                                        )
                                    timezone_picker = self._build_timezone_picker(
                                        ui=ui,
                                        value=current_settings.timestamp.timezone_name,
                                        field_classes=(
                                            "mod-app-details-field mod-config-input mod-timestamp-input w-full min-w-0"
                                        ),
                                        on_change=lambda _: _handle_timezone_change(),
                                    )
                                    timezone_input = timezone_picker.input
                                ui.label(
                                    "The timezone is used only when the date and time does not include one."
                                ).classes("mod-stat-label text-xs")
                                with ui.row().classes("w-full gap-2 flex-wrap"):
                                    ui.button("Now", on_click=lambda _: _apply_exact_preset("now")).classes(
                                        "mod-list-button secondary"
                                    )
                                    ui.button(
                                        "Tomorrow 9:00", on_click=lambda _: _apply_exact_preset("tomorrow")
                                    ).classes("mod-list-button secondary")
                            with ui.tab_panel(relative_tab).classes("w-full mod-app-details-section"):
                                ui.label("Use a duration from now, such as 2h, 3d, 1w, or 1:30.").classes(
                                    "mod-stat-label"
                                )
                                with ui.element("div").classes("w-full grid grid-cols-1 md:grid-cols-2 gap-2"):
                                    relative_time_input = (
                                        ui.input("In")
                                        .props(
                                            "filled square dense stack-label hide-bottom-space inputmode=text "
                                            "placeholder='2h, 3d, or 1w2d'"
                                        )
                                        .classes(
                                            "mod-app-details-field mod-config-input mod-timestamp-input w-full min-w-0"
                                        )
                                    )
                                    rounding_input = (
                                        ui.select(
                                            _TIMESTAMP_ROUNDING_LABELS,
                                            value=current_settings.timestamp.rounding_unit,
                                            label="Round target to nearest",
                                        )
                                        .props(
                                            "filled square dense stack-label hide-bottom-space color=accent options-dark "
                                            "popup-content-class=mod-setting-menu"
                                        )
                                        .classes(
                                            "mod-app-details-field mod-config-select mod-timestamp-input w-full min-w-0"
                                        )
                                    )
                                with ui.row().classes("w-full gap-2 flex-wrap"):
                                    for label, value in (("+1 hour", "1h"), ("+1 day", "1d"), ("+1 week", "1w")):
                                        ui.button(
                                            label,
                                            on_click=lambda _, value=value: _apply_relative_preset(value),
                                        ).classes("mod-list-button secondary")
                        format_input = (
                            ui.select(format_options, value=current_settings.timestamp.format_template, label="Format")
                            .props(
                                "filled square dense stack-label hide-bottom-space color=accent options-dark "
                                "popup-content-class=mod-setting-menu"
                            )
                            .classes("mod-app-details-field mod-config-select mod-timestamp-input w-full min-w-0")
                        )
                        format_input.add_slot(
                            "option",
                            template=r"""
                                <q-item v-bind="props.itemProps" class="mod-timestamp-format-option">
                                    <q-item-section>
                                        <q-item-label>{{ props.opt.label.split(' · ')[0] }}</q-item-label>
                                    </q-item-section>
                                    <q-item-section side class="mod-timestamp-format-pattern">
                                        {{ props.opt.label.split(' · ')[1] }}
                                    </q-item-section>
                                </q-item>
                            """,
                        )
                        result_input = (
                            ui.input("Discord timestamp")
                            .props("filled square dense stack-label readonly hide-bottom-space")
                            .classes("mod-app-details-field mod-config-input mod-timestamp-input w-full")
                        )
                        with result_input.add_slot("append"):
                            copy_timestamp_button = ui.button("", color=None).props(
                                "icon=content_copy flat dense aria-label=Copy timestamp"
                            )
                        result_preview = ui.label("Enter a time to generate a timestamp.").classes("mod-stat-label")
                        with ui.dialog() as date_time_picker_dialog:
                            with ui.card().classes("mod-card mod-dialog-card mod-timestamp-picker-dialog-card"):
                                with ui.column().classes("w-full gap-3"):
                                    ui.label("Choose date and time").classes("text-lg font-black mod-title-small")
                                    with ui.element("div").classes("mod-timestamp-picker-workspace"):
                                        date_picker = (
                                            ui.date(value=datetime.now().date().isoformat())
                                            .props("flat square")
                                            .classes("mod-timestamp-picker-date")
                                        )
                                        time_picker = (
                                            ui.time(value=datetime.now().strftime("%H:%M"))
                                            .props("flat square mask=HH:mm")
                                            .classes("mod-timestamp-picker-time")
                                        )
                                        time_picker._props["format24h"] = current_settings.web_chat.use_24_hour_time
                                    with ui.row().classes("w-full justify-end mod-app-details-actions"):
                                        ui.button("Cancel", on_click=date_time_picker_dialog.close).classes(
                                            "mod-list-button secondary"
                                        )
                                        ui.button(
                                            "Use date and time", on_click=lambda _: _apply_date_time_picker()
                                        ).classes("mod-list-button")

                        def _save_timestamp_preferences(*, notify: bool = False) -> bool:
                            nonlocal current_settings
                            try:
                                timestamp_settings = ModWebTimestampSettings(
                                    timezone_name=_value_as_text(timezone_input),
                                    format_template=_value_as_text(format_input),
                                    rounding_unit=_value_as_text(rounding_input),
                                )
                                next_settings = ModWebUserSettings(
                                    appearance=current_settings.appearance,
                                    web_chat=current_settings.web_chat,
                                    timestamp=timestamp_settings,
                                    country=current_settings.country,
                                )
                                changed = self._backend.save_user_settings(
                                    user_id=user.discord_id,
                                    settings=next_settings,
                                )
                            except (TypeError, ValueError) as xcp:
                                result_preview.set_text(str(xcp))
                                return False
                            except Exception as xcp:
                                log.warning(
                                    "Timestamp preference update failed: user=%s error=%s", user.discord_id, xcp
                                )
                                result_preview.set_text(f"Timestamp preferences were not saved: {xcp}")
                                return False
                            current_settings = next_settings
                            if notify and changed:
                                ui.notify("Timestamp preferences saved.", type="positive")
                            return True

                        def _discord_preview_text(
                            *, timestamp: datetime, format_template: str, use_24_hour_time: bool
                        ) -> str:
                            style = format_template[-2]
                            hour_24 = timestamp.strftime("%H")
                            hour_12 = timestamp.strftime("%I").lstrip("0") or "12"
                            short_time = (
                                f"{hour_24}:{timestamp:%M}"
                                if use_24_hour_time
                                else f"{hour_12}:{timestamp:%M} {timestamp:%p}"
                            )
                            long_time = (
                                f"{short_time}:{timestamp:%S}"
                                if use_24_hour_time
                                else f"{hour_12}:{timestamp:%M:%S} {timestamp:%p}"
                            )
                            short_date = timestamp.strftime("%d/%m/%Y")
                            long_date = f"{timestamp:%B} {timestamp.day}, {timestamp.year}"
                            full_date = f"{timestamp:%A}, {long_date}"
                            if style == "t":
                                return short_time
                            if style == "T":
                                return long_time
                            if style == "d":
                                return short_date
                            if style == "D":
                                return long_date
                            if style == "f":
                                return f"{long_date} {short_time}"
                            if style == "F":
                                return f"{full_date} {short_time}"
                            if style == "s":
                                return f"{timestamp:%Y-%m-%d} {short_time}"
                            if style == "S":
                                return f"{timestamp:%Y-%m-%d} {long_time}"
                            relative_seconds = int((timestamp - datetime.now(timezone.utc)).total_seconds())
                            unit, divisor = (
                                ("day", 86_400)
                                if abs(relative_seconds) >= 86_400
                                else ("hour", 3_600)
                                if abs(relative_seconds) >= 3_600
                                else ("minute", 60)
                                if abs(relative_seconds) >= 60
                                else ("second", 1)
                            )
                            amount = max(1, abs(relative_seconds) // divisor)
                            return f"{amount} {unit}{'' if amount == 1 else 's'} {'from now' if relative_seconds >= 0 else 'ago'}"

                        def _timezone_name_from_timestamp(timestamp: datetime, *, fallback: str) -> str:
                            timezone_key = getattr(timestamp.tzinfo, "key", None)
                            if isinstance(timezone_key, str):
                                normalized_timezone = Utilities.normalise_timezone_name(timezone_key)
                                if normalized_timezone is not None:
                                    return normalized_timezone
                            offset = timestamp.utcoffset()
                            if offset is None:
                                return fallback
                            total_minutes = int(offset.total_seconds() // 60)
                            sign = "+" if total_minutes >= 0 else "-"
                            hours, minutes = divmod(abs(total_minutes), 60)
                            return f"UTC{sign}{hours:02d}:{minutes:02d}"

                        def _sync_timezone_from_exact_timestamp(
                            *, timestamp: datetime, parsed_timezone: tzinfo
                        ) -> None:
                            if timestamp.tzinfo == parsed_timezone:
                                return
                            timezone_picker.set_timezone(
                                _timezone_name_from_timestamp(
                                    timestamp,
                                    fallback=_value_as_text(timezone_input),
                                )
                            )
                            _save_timestamp_preferences()

                        def _open_date_time_picker(_: object | None = None) -> None:
                            timezone_name = _value_as_text(timezone_input)
                            parsed_timezone = Utilities.parse_timezone(timezone_name)
                            if parsed_timezone is None:
                                result_preview.set_text(f"Unknown timezone: {timezone_name.strip() or 'blank'}")
                                return
                            parsed_time = Utilities.parse_exact_time(
                                _value_as_text(exact_time_input), tz=parsed_timezone
                            )
                            selected_time = datetime.now(parsed_timezone) if parsed_time is None else parsed_time
                            date_picker.set_value(selected_time.date().isoformat())
                            time_picker.set_value(selected_time.strftime("%H:%M"))
                            date_time_picker_dialog.open()

                        def _apply_date_time_picker() -> None:
                            try:
                                selected_date = date.fromisoformat(_value_as_text(date_picker))
                                selected_time = datetime.strptime(_value_as_text(time_picker), "%H:%M").time()
                            except ValueError:
                                result_preview.set_text("Choose a valid date and time.")
                                return
                            exact_time_input.set_value(
                                datetime.combine(selected_date, selected_time).isoformat(timespec="minutes")
                            )
                            date_time_picker_dialog.close()
                            _update_timestamp()

                        def _update_timestamp(_: object | None = None) -> None:
                            try:
                                format_template = _value_as_text(format_input)
                                if format_template not in format_options:
                                    raise ValueError("Choose a timestamp format.")
                                if active_mode == "exact":
                                    timezone_name = _value_as_text(timezone_input).strip()
                                    parsed_timezone = Utilities.parse_timezone(timezone_name)
                                    if parsed_timezone is None:
                                        raise ValueError(f"Unknown timezone: {timezone_name or 'blank'}")
                                    timestamp = Utilities.parse_exact_time(
                                        _value_as_text(exact_time_input), tz=parsed_timezone
                                    )
                                    if timestamp is None:
                                        raise ValueError("Enter an exact date and time.")
                                    _sync_timezone_from_exact_timestamp(
                                        timestamp=timestamp,
                                        parsed_timezone=parsed_timezone,
                                    )
                                else:
                                    timestamp = Utilities.parse_relative_time(
                                        _value_as_text(relative_time_input), tz=timezone.utc
                                    )
                                    if timestamp is None:
                                        raise ValueError("Enter a relative duration, such as 2h or 1w2d.")
                                    timestamp = Utilities.round_wallclock(timestamp, _value_as_text(rounding_input))
                            except (TypeError, ValueError) as xcp:
                                result_input.set_value("")
                                result_preview.set_text(str(xcp))
                                return
                            rounded_utc = timestamp.astimezone(timezone.utc)
                            epoch = int(rounded_utc.timestamp())
                            result_input.set_value(format_template.format(epoch))
                            result_preview.set_text(
                                "24h: "
                                f"{
                                    _discord_preview_text(
                                        timestamp=rounded_utc,
                                        format_template=format_template,
                                        use_24_hour_time=True,
                                    )
                                }"
                                "  |  12h: "
                                f"{
                                    _discord_preview_text(
                                        timestamp=rounded_utc,
                                        format_template=format_template,
                                        use_24_hour_time=False,
                                    )
                                }"
                            )

                        def _update_timestamp_preferences(event: object | None = None) -> None:
                            _update_timestamp(event)
                            _save_timestamp_preferences()

                        def _handle_timezone_change() -> None:
                            if active_mode == "exact":
                                _update_timestamp()
                            if Utilities.normalise_timezone_name(_value_as_text(timezone_input)) is not None:
                                _save_timestamp_preferences()

                        def _handle_mode_change(event: object) -> None:
                            nonlocal active_mode
                            next_mode = _value_as_text(event)
                            if next_mode not in {"exact", "relative"}:
                                return
                            active_mode = cast(_TimestampInputMode, next_mode)
                            _update_timestamp()

                        def _apply_exact_preset(value: str) -> None:
                            if value == "now":
                                parsed_timezone = Utilities.parse_timezone(_value_as_text(timezone_input))
                                if parsed_timezone is None:
                                    result_preview.set_text("Choose a valid timezone before using this preset.")
                                    return
                                exact_time_input.set_value(datetime.now(parsed_timezone).strftime("%Y-%m-%dT%H:%M:%S"))
                            elif value == "tomorrow":
                                parsed_timezone = Utilities.parse_timezone(_value_as_text(timezone_input))
                                if parsed_timezone is None:
                                    result_preview.set_text("Choose a valid timezone before using this preset.")
                                    return
                                tomorrow = (datetime.now(parsed_timezone) + timedelta(days=1)).replace(
                                    hour=9,
                                    minute=0,
                                    second=0,
                                    microsecond=0,
                                )
                                exact_time_input.set_value(tomorrow.strftime("%Y-%m-%dT%H:%M"))
                            else:
                                raise ValueError(f"Unknown timestamp preset: {value}")
                            _update_timestamp()

                        def _apply_relative_preset(value: str) -> None:
                            relative_time_input.set_value(value)
                            _update_timestamp()

                        async def _copy_timestamp(_: object | None = None) -> None:
                            timestamp_token = _value_as_text(result_input)
                            if not timestamp_token:
                                ui.notify("Generate a timestamp before copying it.", type="warning")
                                return
                            script_result = ui.run_javascript(
                                f"return navigator.clipboard.writeText({json.dumps(timestamp_token)});",
                                timeout=1.0,
                            )
                            if inspect.isawaitable(script_result):
                                await cast(Awaitable[object], script_result)
                            ui.notify("Timestamp copied.", type="positive")

                        exact_time_input.on("update:value", _update_timestamp)
                        relative_time_input.on("update:value", _update_timestamp)
                        copy_timestamp_button.on("click", _copy_timestamp)
                        date_time_picker_button.on("click", _open_date_time_picker)
                        for control in (format_input, rounding_input):
                            control.on("update:model-value", _update_timestamp_preferences)
                        with ui.row().classes("w-full justify-end mod-app-details-actions"):
                            ui.button("Close", on_click=time_dialog.close).classes("mod-list-button secondary")
                time_dialog.open()

        return _show_time_formatter_panel

    def _build_currency_converter_panel(self, *, ui: ModWebUi, user: ModWebUser) -> Callable[[], None]:
        currency_options: dict[str, str] = {currency.name: currency.name for currency in config.SUPPORTED_CURRENCY}
        currency_names: tuple[str, ...] = tuple(currency.name for currency in config.SUPPORTED_CURRENCY)
        table_columns: list[dict[str, str]] = [
            {"name": "symbol", "label": "", "field": "symbol", "align": "center"},
            {"name": "currency", "label": "Currency", "field": "currency", "align": "left"},
            {"name": "amount", "label": "Converted amount", "field": "amount", "align": "right"},
        ]

        def _table_rows(*, amounts: Mapping[config.Currency, Decimal] | None) -> list[dict[str, str]]:
            return [
                {
                    "symbol": currency.symbol,
                    "currency": currency.name,
                    "amount": "—" if amounts is None else f"{amounts[currency]:,.3f}",
                }
                for currency in config.SUPPORTED_CURRENCY
            ]

        def _rate_summary(*, provider: str, as_of: object, age_days: int) -> str:
            if not isinstance(as_of, date):
                return "Rates have not been loaded yet."
            stale_detail = f" · {age_days} days old" if age_days >= 7 else ""
            return f"{provider} rates as of {as_of:%-d %b}{stale_detail}"

        def _selected_currency_name(value_container: object) -> str:
            raw_value = _value_as_object(value_container)
            if isinstance(raw_value, dict):
                raw_label = raw_value.get("label")
                if isinstance(raw_label, str) and raw_label in config.Currency.__members__:
                    return raw_label
                raw_value = raw_value.get("value")
            if isinstance(raw_value, str) and raw_value in config.Currency.__members__:
                return raw_value
            if isinstance(raw_value, int) and not isinstance(raw_value, bool) and 0 <= raw_value < len(currency_names):
                return currency_names[raw_value]
            raise ValueError("Choose a supported currency.")

        def _show_currency_converter_panel() -> None:
            country = self._backend.user_settings_for(user_id=user.discord_id).country
            source_currency = (
                config.CURRENCY_COUNTRIES.get(country, config.Currency.AUD)
                if country is not None
                else config.Currency.AUD
            )
            initial_amount = Decimal("1")
            initial_batch = CurrencyConverter.cached_ecb_conversion_batch(amount=initial_amount, src=source_currency)
            with ui.dialog() as currency_dialog:
                with ui.card().classes("mod-card mod-dialog-card mod-app-details-dialog-card"):
                    with ui.column().classes("w-full gap-4 mod-app-details-layout"):
                        with ui.column().classes("gap-1"):
                            ui.label("Currency").classes("text-xl font-black mod-title-small")
                            ui.label(
                                "Compare an amount across every supported currency. "
                                "Amounts support addition, subtraction, and percentage adjustments."
                            ).classes("mod-stat-label")
                        with ui.element("div").classes("w-full grid grid-cols-2 gap-2 items-end"):
                            amount_input = (
                                ui.input("Amount", value="1")
                                .props("filled square dense hide-bottom-space inputmode=decimal")
                                .classes("mod-app-details-field mod-config-input col-span-1 min-w-0")
                            )
                            source_input = (
                                ui.select(currency_options, value=source_currency.name, label="From")
                                .props(
                                    "filled square dense hide-bottom-space color=accent options-dark "
                                    "popup-content-class=mod-setting-menu"
                                )
                                .classes("mod-app-details-field mod-config-select col-span-1 min-w-0")
                            )
                        initial_summary = (
                            "Loading ECB reference rates…"
                            if initial_batch is None
                            else _rate_summary(
                                provider=initial_batch.provider.value,
                                as_of=initial_batch.as_of,
                                age_days=initial_batch.age.days,
                            )
                        )
                        initial_summary_classes = (
                            "mod-stat-label text-warning"
                            if (initial_batch is not None and initial_batch.is_stale)
                            else "mod-stat-label"
                        )
                        rate_summary_label = ui.label(initial_summary).classes(initial_summary_classes)
                        conversion_table = (
                            ui.table(
                                columns=table_columns,
                                rows=_table_rows(
                                    amounts=None if initial_batch is None else initial_batch.amounts,
                                ),
                                row_key="currency",
                            )
                            .props("dense flat hide-bottom")
                            .classes("w-full mod-currency-table")
                        )

                        async def _update_ecb_rates(
                            _: object | None = None,
                            *,
                            amount_text: str | None = None,
                            source_name: str | None = None,
                        ) -> None:
                            try:
                                parsed_amount = CurrencyConverter.parse_amount(
                                    _value_as_text(amount_input) if amount_text is None else amount_text
                                )
                                if parsed_amount is None or parsed_amount.amount == 0:
                                    raise ValueError("Enter a non-zero amount.")
                                source = config.Currency[
                                    _selected_currency_name(source_input) if source_name is None else source_name
                                ]
                                conversion_batch = await CurrencyConverter.convert_all_with_ecb_metadata(
                                    amount=parsed_amount.amount,
                                    src=source,
                                )
                                if conversion_batch is None:
                                    raise RuntimeError("ECB reference rates are unavailable. Please try again shortly.")
                            except (TypeError, ValueError, RuntimeError) as xcp:
                                rate_summary_label.set_text(str(xcp))
                                return
                            rate_summary_label.set_text(
                                _rate_summary(
                                    provider=conversion_batch.provider.value,
                                    as_of=conversion_batch.as_of,
                                    age_days=conversion_batch.age.days,
                                )
                            )
                            if conversion_batch.is_stale:
                                rate_summary_label.classes(add="text-warning")
                            else:
                                rate_summary_label.classes(remove="text-warning")
                            conversion_table.rows = _table_rows(amounts=conversion_batch.amounts)
                            conversion_table.update()
                            fallback_section.set_visibility(conversion_batch.is_stale)

                        with ui.column() as fallback_section:
                            fallback_section.classes("w-full gap-2 mod-app-details-subsection")
                            ui.label("Live fallback").classes("text-base font-black mod-title-small")
                            ui.label(
                                "ECB reference rates are over seven days old. Fetch one current conversion from the "
                                "backup service."
                            ).classes("mod-stat-label")
                            with ui.element("div").classes("w-full grid grid-cols-2 gap-2 items-end"):
                                fallback_target_input = (
                                    ui.select(
                                        currency_options,
                                        value=(
                                            config.Currency.USD.name
                                            if source_currency is not config.Currency.USD
                                            else config.Currency.AUD.name
                                        ),
                                        label="To",
                                    )
                                    .props(
                                        "filled square dense hide-bottom-space color=accent options-dark "
                                        "popup-content-class=mod-setting-menu"
                                    )
                                    .classes("mod-app-details-field mod-config-select col-span-1 min-w-0")
                                )
                                fallback_result_label = ui.label("").classes("mod-stat-label col-span-1")

                            async def _fetch_fallback(_: object | None = None) -> None:
                                try:
                                    parsed_amount = CurrencyConverter.parse_amount(_value_as_text(amount_input))
                                    if parsed_amount is None or parsed_amount.amount == 0:
                                        raise ValueError("Enter a non-zero amount.")
                                    source = config.Currency[_selected_currency_name(source_input)]
                                    target = config.Currency[_selected_currency_name(fallback_target_input)]
                                    conversion = await CurrencyConverter.fetch_with_metadata(
                                        amount=parsed_amount.amount,
                                        src=source,
                                        dst=target,
                                    )
                                    if conversion is None:
                                        raise RuntimeError("The backup rate is unavailable. Please try again shortly.")
                                except (TypeError, ValueError, RuntimeError) as xcp:
                                    fallback_result_label.set_text(str(xcp))
                                    return
                                fallback_result_label.set_text(
                                    f"{conversion.amount:,.3f} {target.name} · {conversion.provider.value}"
                                )

                            ui.button("Fetch", icon="download", on_click=_fetch_fallback).classes("mod-list-button")
                        fallback_section.set_visibility(initial_batch is not None and initial_batch.is_stale)

                        amount_input.on(
                            "update:model-value",
                            lambda event: _update_ecb_rates(amount_text=_value_as_text(event)),
                        )
                        source_input.on(
                            "update:model-value",
                            lambda event: _update_ecb_rates(source_name=_selected_currency_name(event)),
                        )

                        with ui.row().classes("w-full justify-end mod-app-details-actions"):
                            if config.INDEV:
                                ui.button(
                                    "Live fallback",
                                    icon="science",
                                    on_click=lambda _: fallback_section.set_visibility(True),
                                ).classes("mod-list-button secondary")
                            ui.button("Close", on_click=currency_dialog.close).classes("mod-list-button secondary")
                currency_dialog.open()
            ui.timer(0.1, lambda: asyncio.create_task(_update_ecb_rates()), once=True)

        return _show_currency_converter_panel
