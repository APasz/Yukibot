"""Unit-conversion UI helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Protocol, cast

from unit_conversion import (
    UnitCategory,
    UnitConversion,
    UnitDefinition,
    convert_unit_category,
    display_units_for_category,
    format_unit_amount,
    parse_unit_amount,
    unit_categories,
    unit_definition,
    units_for_category,
)

from .nicegui_protocols import ModWebUi, _value_as_object, _value_as_text
from .status_support import ModWebStatusFeatureSupport


class _UnitSelector(Protocol):
    def set_options(self, options: dict[str, str], *, value: str | None = None) -> None: ...


class ModWebStatusUnitConverterMixin(ModWebStatusFeatureSupport):
    def _build_unit_converter_panel(self, *, ui: ModWebUi) -> Callable[[], None]:
        category_options: dict[str, str] = {category.value: category.display_name for category in unit_categories()}
        categories_by_display_name: dict[str, UnitCategory] = {
            category.display_name: category for category in unit_categories()
        }
        category_values: tuple[UnitCategory, ...] = unit_categories()
        table_columns: list[dict[str, str]] = [
            {"name": "unit", "label": "Unit", "field": "unit", "align": "left"},
            {"name": "system", "label": "System", "field": "system", "align": "left"},
            {
                "name": "amount",
                "label": "Converted amount",
                "field": "amount",
                "align": "right",
                "summary": "",
            },
        ]

        def _unit_options(category: UnitCategory) -> dict[str, str]:
            return {
                unit.code: f"{unit.display_name} · {unit.system.display_name}" for unit in units_for_category(category)
            }

        def _table_rows(
            *,
            category: UnitCategory,
            conversions: tuple[UnitConversion, ...],
            unit_filter_text: str = "",
            system_filter_text: str = "",
        ) -> list[dict[str, str]]:
            converted_amounts = {conversion.target.code: conversion.converted_amount for conversion in conversions}
            normalised_unit_filter = unit_filter_text.strip().casefold()
            normalised_system_filter = system_filter_text.strip().casefold()
            return [
                {
                    "unit": unit.display_name,
                    "system": unit.system.display_name,
                    "amount": f"{format_unit_amount(converted_amounts[unit.code])} {unit.symbol}",
                }
                for unit in display_units_for_category(category)
                if normalised_unit_filter in f"{unit.name} {unit.symbol}".casefold()
                if normalised_system_filter in unit.system.display_name.casefold()
            ]

        def _selected_category(value_container: object) -> UnitCategory:
            raw_value = _value_as_object(value_container)
            candidates: list[object] = [raw_value]
            if isinstance(raw_value, Mapping):
                selected_values = cast(Mapping[str, object], raw_value)
                candidates = [selected_values.get("value"), selected_values.get("label")]
            for candidate in candidates:
                if isinstance(candidate, int) and not isinstance(candidate, bool):
                    if 0 <= candidate < len(category_values):
                        return category_values[candidate]
                    continue
                if isinstance(candidate, str):
                    if candidate in categories_by_display_name:
                        return categories_by_display_name[candidate]
                    try:
                        return UnitCategory(candidate)
                    except ValueError:
                        continue
            raise ValueError("Choose a measurement category.")

        def _selected_unit(category: UnitCategory, value_container: object) -> UnitDefinition:
            unit_options = _unit_options(category)
            labels_to_codes = {label: code for code, label in unit_options.items()}
            units = units_for_category(category)
            raw_value = _value_as_object(value_container)
            candidates: list[object] = [raw_value]
            if isinstance(raw_value, Mapping):
                selected_values = cast(Mapping[str, object], raw_value)
                candidates = [selected_values.get("value"), selected_values.get("label")]
            for candidate in candidates:
                if isinstance(candidate, int) and not isinstance(candidate, bool):
                    if 0 <= candidate < len(units):
                        return units[candidate]
                    continue
                if isinstance(candidate, str):
                    code = labels_to_codes.get(candidate, candidate)
                    try:
                        return unit_definition(category=category, code=code)
                    except ValueError:
                        continue
            raise ValueError(f"Choose a {category.display_name.lower()} unit.")

        def _show_unit_converter_panel() -> None:
            initial_category = UnitCategory.LENGTH
            initial_source = unit_definition(category=initial_category, code="m")
            initial_amount = Decimal("1")
            initial_conversions = convert_unit_category(amount=initial_amount, source=initial_source)
            current_conversions = initial_conversions
            unit_filter_text = ""
            system_filter_text = ""
            filter_rows_handler = r"""() => {
                const tableRoot = $el.closest('.q-table') || $el;
                const table = tableRoot.matches('table') ? tableRoot : tableRoot.querySelector('table');
                if (!table) return;
                const unitFilter = (table.querySelector('.mod-unit-conversion-unit-filter input')?.value || '').trim().toLocaleLowerCase();
                const systemFilter = (table.querySelector('.mod-unit-conversion-system-filter input')?.value || '').trim().toLocaleLowerCase();
                Array.from(table.tBodies[0]?.rows || []).forEach((row) => {
                    const unit = row.cells[0]?.textContent?.toLocaleLowerCase() || '';
                    const system = row.cells[1]?.textContent?.toLocaleLowerCase() || '';
                    row.style.display = unit.includes(unitFilter) && system.includes(systemFilter) ? '' : 'none';
                });
            }"""
            with ui.dialog() as unit_converter_dialog:
                with ui.card().classes("mod-card mod-dialog-card mod-app-details-dialog-card"):
                    with ui.column().classes("w-full gap-4 mod-app-details-layout"):
                        with ui.column().classes("gap-1"):
                            ui.label("Unit converter").classes("text-xl font-black mod-title-small")
                            ui.label(
                                "Convert metric, scientific, US customary, UK imperial, and other common units. "
                                "Choose a category to compare every compatible unit."
                            ).classes("mod-stat-label")
                        with ui.element("div").classes("w-full grid grid-cols-3 gap-2 items-end"):
                            category_input = (
                                ui.select(category_options, value=initial_category.value, label="Category")
                                .props(
                                    "filled square dense hide-bottom-space color=accent options-dark "
                                    "popup-content-class=mod-setting-menu"
                                )
                                .classes("mod-app-details-field mod-config-select col-span-1 min-w-0")
                            )
                            amount_input = (
                                ui.input("Amount", value="1")
                                .props("filled square dense hide-bottom-space inputmode=decimal")
                                .classes("mod-app-details-field mod-config-input col-span-1 min-w-0")
                            )
                            source_input = (
                                ui.select(_unit_options(initial_category), value=initial_source.code, label="From")
                                .props(
                                    "filled square dense hide-bottom-space color=accent options-dark "
                                    "popup-content-class=mod-setting-menu"
                                )
                                .classes("mod-app-details-field mod-config-select col-span-1 min-w-0")
                            )
                            source_input.add_slot(
                                "option",
                                template=r"""
                                    <q-item v-bind="props.itemProps" class="mod-unit-conversion-option">
                                        <q-item-section>
                                            <q-item-label>{{ props.opt.label.split(' · ')[0] }}</q-item-label>
                                        </q-item-section>
                                        <q-item-section side class="mod-unit-conversion-system">
                                            {{ props.opt.label.split(' · ')[1] }}
                                        </q-item-section>
                                    </q-item>
                                """,
                            )
                        table_columns[2]["summary"] = (
                            f"{format_unit_amount(initial_amount)} {initial_source.symbol} · {initial_source.name}"
                        )
                        conversion_table = (
                            ui.table(
                                columns=table_columns,
                                rows=_table_rows(category=initial_category, conversions=initial_conversions),
                                row_key="unit",
                            )
                            .props("dense flat hide-bottom")
                            .classes("w-full mod-unit-conversion-table")
                        )
                        conversion_table.add_slot(
                            "header-cell-unit",
                            f"""
                                <q-th :props="props" class="mod-unit-conversion-filter-heading">
                                    <q-input dense borderless clearable placeholder="Filter unit" @click.stop
                                             class="mod-unit-conversion-unit-filter"
                                             @update:model-value="{filter_rows_handler}"
                                             @blur="$parent.$emit('unit-filter', $event.target.value)" />
                                </q-th>
                            """,
                        )
                        conversion_table.add_slot(
                            "header-cell-system",
                            f"""
                                <q-th :props="props" class="mod-unit-conversion-filter-heading">
                                    <q-input dense borderless clearable placeholder="Filter system" @click.stop
                                             class="mod-unit-conversion-system-filter"
                                             @update:model-value="{filter_rows_handler}"
                                             @blur="$parent.$emit('system-filter', $event.target.value)" />
                                </q-th>
                            """,
                        )
                        conversion_table.add_slot(
                            "header-cell-amount",
                            r"""
                                <q-th :props="props" class="mod-unit-conversion-amount-heading">
                                    <div class="mod-unit-conversion-source">{{ props.col.summary }}</div>
                                    <div>{{ props.col.label }}</div>
                                </q-th>
                            """,
                        )

                        def _update_table() -> None:
                            conversion_table.rows = _table_rows(
                                category=current_conversions[0].source.category,
                                conversions=current_conversions,
                                unit_filter_text=unit_filter_text,
                                system_filter_text=system_filter_text,
                            )
                            conversion_table.update()

                        def _update_unit_filter(event: object | None = None) -> None:
                            nonlocal unit_filter_text
                            unit_filter_text = "" if event is None else _value_as_text(event)

                        def _update_system_filter(event: object | None = None) -> None:
                            nonlocal system_filter_text
                            system_filter_text = "" if event is None else _value_as_text(event)

                        def _set_conversion_summary(text: str) -> None:
                            table_columns[2]["summary"] = text

                        def _update_conversions(
                            _: object | None = None,
                            *,
                            category: UnitCategory | None = None,
                            amount_text: str | None = None,
                            source: UnitDefinition | None = None,
                        ) -> None:
                            nonlocal current_conversions
                            try:
                                resolved_category = _selected_category(category_input) if category is None else category
                                resolved_amount = parse_unit_amount(
                                    _value_as_text(amount_input) if amount_text is None else amount_text
                                )
                                resolved_source = (
                                    _selected_unit(resolved_category, source_input) if source is None else source
                                )
                                conversions = convert_unit_category(amount=resolved_amount, source=resolved_source)
                            except (TypeError, ValueError) as xcp:
                                _set_conversion_summary(str(xcp))
                                conversion_table.update()
                                return
                            _set_conversion_summary(
                                f"{format_unit_amount(resolved_amount)} {resolved_source.symbol} · "
                                f"{resolved_source.name}"
                            )
                            current_conversions = conversions
                            _update_table()

                        def _change_category(event: object | None = None) -> None:
                            try:
                                category = _selected_category(category_input if event is None else event)
                            except ValueError as xcp:
                                _set_conversion_summary(str(xcp))
                                conversion_table.update()
                                return
                            source = units_for_category(category)[0]
                            source_selector = cast(_UnitSelector, cast(object, source_input))
                            source_selector.set_options(_unit_options(category), value=source.code)
                            _update_conversions(category=category, source=source)

                        amount_input.on(
                            "update:model-value",
                            lambda event: _update_conversions(amount_text=_value_as_text(event)),
                        )
                        category_input.on("update:model-value", _change_category)
                        source_input.on("update:model-value", _update_conversions)
                        conversion_table.on("unit-filter", _update_unit_filter)
                        conversion_table.on("system-filter", _update_system_filter)
                        with ui.row().classes("w-full justify-end mod-app-details-actions"):
                            ui.button("Close", on_click=unit_converter_dialog.close).classes(
                                "mod-list-button secondary"
                            )
                unit_converter_dialog.open()

        return _show_unit_converter_panel
