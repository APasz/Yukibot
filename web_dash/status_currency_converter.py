"""Currency-conversion UI helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal
from typing import cast

import config
from currency_conversion import CurrencyConverter
from mod_web_auth import ModWebUser

from .nicegui_protocols import ModWebUi, _value_as_object, _value_as_text
from .status_support import ModWebStatusFeatureSupport


class ModWebStatusCurrencyConverterMixin(ModWebStatusFeatureSupport):
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
                selected_values = cast(dict[str, object], raw_value)
                raw_label = selected_values.get("label")
                if isinstance(raw_label, str) and raw_label in config.Currency.__members__:
                    return raw_label
                raw_value = selected_values.get("value")
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
                                def _show_live_fallback(_: object) -> None:
                                    fallback_section.set_visibility(True)

                                ui.button(
                                    "Live fallback",
                                    icon="science",
                                    on_click=_show_live_fallback,
                                ).classes("mod-list-button secondary")
                            ui.button("Close", on_click=currency_dialog.close).classes("mod-list-button secondary")
                currency_dialog.open()
            ui.timer(0.1, lambda: asyncio.create_task(_update_ecb_rates()), once=True)

        return _show_currency_converter_panel
