"""Standard-drink conversion UI helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from nicegui.element import Element
from nicegui.elements.button import Button
from nicegui.elements.input import Input

import config
from mod_web_auth import ModWebUser
from standard_drinks import (
    beverage_standard_drink_estimate,
    format_standard_drink_definition,
    format_standard_drink_number,
    format_standard_drink_range,
    parse_standard_drink_expression,
    standard_drink_conversion,
    standard_drink_definition,
    standard_drink_equivalents,
    standard_drink_units,
)

from .nicegui_protocols import ModWebUi, _value_as_text
from .status_support import ModWebStatusFeatureSupport


@dataclass(slots=True)
class _StandardDrinkEstimateInputRow:
    container: Element
    volume_input: Input
    abv_input: Input
    remove_button: Button


class ModWebStatusStandardDrinksMixin(ModWebStatusFeatureSupport):
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

                            def _remove_row_handler(
                                reference: list[_StandardDrinkEstimateInputRow],
                            ) -> Callable[[object], None]:
                                def _on_click(_: object) -> None:
                                    if not reference:
                                        raise RuntimeError("Estimate row is not initialized.")
                                    _remove_estimate_row(reference[0])

                                return _on_click

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
                                            on_click=_remove_row_handler(estimate_row_reference),
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
                                lambda event: _update_from_standard_drinks(amount_text=_value_as_text(event)),
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
