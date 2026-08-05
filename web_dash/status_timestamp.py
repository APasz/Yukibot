"""Discord timestamp UI helpers."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Literal, cast

from _utils import Utilities
from mod_web_auth import ModWebUser

from .constants import log
from .nicegui_protocols import ModWebUi, _value_as_text
from .status_timezone import ModWebStatusTimezoneMixin
from .user_settings import ModWebTimestampSettings, ModWebUserSettings

_TIMESTAMP_ROUNDING_LABELS: dict[str, str] = {
    "Y": "Year",
    "MO": "Month",
    "W": "Week",
    "D": "Day",
    "H": "Hour",
    "MI": "Minute",
    "S": "Second",
}
_TimestampInputMode = Literal["exact", "relative"]


class ModWebStatusTimestampMixin(ModWebStatusTimezoneMixin):
    def _build_time_formatter_panel(self, *, ui: ModWebUi, user: ModWebUser) -> Callable[[], None]:
        format_options: dict[str, str] = {
            template: f"{label} · {Utilities.DISCORD_TIMESTAMP_STYLE_REPRESENTATIONS[template[-2]]}"
            for label, template in Utilities.DISCORD_TIMESTAMP_FORMATS
        }

        def _show_time_formatter_panel() -> None:
            current_settings = self._backend.user_settings_for(user_id=user.discord_id)
            active_mode: _TimestampInputMode = "exact"

            def _handle_mode_change(event: object) -> None:
                nonlocal active_mode
                next_mode = _value_as_text(event)
                if next_mode not in {"exact", "relative"}:
                    return
                active_mode = cast(_TimestampInputMode, next_mode)
                _update_timestamp()

            def _handle_timezone_picker_change(_: str) -> None:
                _handle_timezone_change()

            def _exact_preset_handler(value: str) -> Callable[[object], None]:
                def _on_click(_: object) -> None:
                    _apply_exact_preset(value)

                return _on_click

            def _relative_preset_handler(value: str) -> Callable[[object], None]:
                def _on_click(_: object) -> None:
                    _apply_relative_preset(value)

                return _on_click

            def _apply_date_time_picker_handler(_: object) -> None:
                _apply_date_time_picker()

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
                            ui.tabs(value="exact", on_change=_handle_mode_change)
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
                                        on_change=_handle_timezone_picker_change,
                                    )
                                    timezone_input = timezone_picker.input
                                ui.label(
                                    "The timezone is used only when the date and time does not include one."
                                ).classes("mod-stat-label text-xs")
                                with ui.row().classes("w-full gap-2 flex-wrap"):
                                    ui.button("Now", on_click=_exact_preset_handler("now")).classes(
                                        "mod-list-button secondary"
                                    )
                                    ui.button(
                                        "Tomorrow 9:00", on_click=_exact_preset_handler("tomorrow")
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
                                            on_click=_relative_preset_handler(value),
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
                                            "Use date and time", on_click=_apply_date_time_picker_handler
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
