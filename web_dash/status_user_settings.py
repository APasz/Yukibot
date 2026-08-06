"""User settings UI helpers."""

from __future__ import annotations

import json
from collections.abc import Callable

import config
from mod_web_auth import ModWebUser

from .constants import log
from .nicegui_protocols import ModWebUi, ModWebValueContainer, _value_as_object, _value_as_text
from .status_appearance import (
    _USER_APPEARANCE_COLOR_SPECS,
    ModWebUserAppearanceMixin,
    _UserAppearanceColorKey,
)
from .status_timezone import ModWebStatusTimezoneMixin
from .user_plate import user_plate_action_icons_by_label, user_plate_action_options
from .user_settings import (
    ModWebChatSettings,
    ModWebTimestampSettings,
    ModWebUserPlateAction,
    ModWebUserPlateSettings,
    ModWebUserSettings,
)

_TIME_FORMAT_OPTIONS: dict[str, str] = {
    "24": "24-hour · 14:30",
    "12": "12-hour · 2:30 PM",
}


class ModWebStatusUserSettingsMixin(ModWebStatusTimezoneMixin, ModWebUserAppearanceMixin):
    def _build_user_settings_panel(self, *, ui: ModWebUi, user: ModWebUser) -> Callable[[], None]:
        def _show_user_settings_panel() -> None:
            current_settings: ModWebUserSettings = self._backend.user_settings_for(user_id=user.discord_id)
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
                                        .classes("mod-app-details-field mod-user-accent-input w-full min-w-0")
                                    )

                            def _capture_color_values() -> dict[_UserAppearanceColorKey, str]:
                                return {
                                    spec.key: self._normalized_user_appearance_color_hex(
                                        _value_as_text(color_inputs[spec.key])
                                    )
                                    for spec in _USER_APPEARANCE_COLOR_SPECS
                                }

                            def _apply_color_values_to_controls(colors: dict[_UserAppearanceColorKey, str]) -> None:
                                for spec in _USER_APPEARANCE_COLOR_SPECS:
                                    set_value = getattr(color_inputs[spec.key], "set_value", None)
                                    if callable(set_value):
                                        set_value(colors[spec.key])

                            tooltip_above_on_touch_input = (
                                ui.checkbox(
                                    "Tooltip above on touch device",
                                    value=current_settings.appearance.tooltip_above_on_touch_device,
                                )
                                .props("dense color=accent")
                                .classes("mod-app-details-field")
                            )

                            def _capture_tooltip_above_on_touch_device() -> bool:
                                value = _value_as_object(tooltip_above_on_touch_input)
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
                            ui.label("User plate").classes("mod-stat-label")
                            ui.label(
                                "Show selected actions beside your profile. They remain available in Utilities."
                            ).classes("mod-subtitle text-xs")
                            current_plate_actions = current_settings.user_plate.visible_actions

                            def _user_plate_selection_summary(action_count: int) -> str:
                                noun = "button" if action_count == 1 else "buttons"
                                return f"{action_count} {noun} selected"

                            user_plate_icons_by_label = json.dumps(user_plate_action_icons_by_label())
                            user_plate_actions_input = (
                                ui.select(
                                    user_plate_action_options(),
                                    value=[action.value for action in current_plate_actions],
                                    label="Buttons",
                                    multiple=True,
                                )
                                .props(
                                    "filled square dense stack-label hide-bottom-space color=accent options-dark "
                                    f'display-value="{_user_plate_selection_summary(len(current_plate_actions))}" '
                                    'popup-content-class="mod-setting-menu mod-user-plate-menu"'
                                )
                                .classes("mod-app-details-field mod-config-select mod-user-plate-select w-full")
                            )
                            user_plate_actions_input.add_slot(
                                "option",
                                f"""
                                <q-item v-bind="props.itemProps">
                                    <q-item-section avatar>
                                        <q-checkbox
                                            :model-value="props.selected"
                                            dense
                                            color="primary"
                                            tabindex="-1"
                                            class="pointer-events-none"
                                        />
                                    </q-item-section>
                                    <q-item-section>
                                        <div class="mod-user-plate-option-content">
                                            <q-icon :name='{user_plate_icons_by_label}[props.opt.label]' />
                                            <q-item-label>{{{{ props.opt.label }}}}</q-item-label>
                                        </div>
                                    </q-item-section>
                                </q-item>
                                """,
                            )

                            def _update_user_plate_selection_summary(_: object) -> None:
                                raw_actions = _value_as_object(user_plate_actions_input)
                                if isinstance(raw_actions, list):
                                    user_plate_actions_input.props(
                                        f'display-value="{_user_plate_selection_summary(len(raw_actions))}"'
                                    )

                            user_plate_actions_input.on(
                                "update:model-value",
                                _update_user_plate_selection_summary,
                            )

                            def _capture_user_plate_settings() -> ModWebUserPlateSettings:
                                raw_actions = _value_as_object(user_plate_actions_input)
                                if not isinstance(raw_actions, list):
                                    raise ValueError("Choose user plate buttons from the list.")
                                visible_actions: list[ModWebUserPlateAction] = []
                                for raw_action in raw_actions:
                                    if not isinstance(raw_action, str):
                                        raise ValueError("Choose user plate buttons from the list.")
                                    try:
                                        visible_actions.append(ModWebUserPlateAction(raw_action))
                                    except ValueError as xcp:
                                        raise ValueError("Choose user plate buttons from the list.") from xcp
                                return ModWebUserPlateSettings(visible_actions=tuple(visible_actions))

                            def _apply_user_plate_settings_to_control(settings: ModWebUserSettings) -> None:
                                visible_actions = settings.user_plate.visible_actions
                                user_plate_actions_input.set_value([action.value for action in visible_actions])
                                user_plate_actions_input.props(
                                    f'display-value="{_user_plate_selection_summary(len(visible_actions))}"'
                                )

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
                                        web_chat=ModWebChatSettings(use_24_hour_time=_capture_use_24_hour_time()),
                                        user_plate=_capture_user_plate_settings(),
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

                                plate_actions_changed = current_settings.user_plate != next_settings.user_plate
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
                                _apply_user_plate_settings_to_control(next_settings)
                                _apply_time_preferences_to_controls(next_settings)
                                _apply_tooltip_placement_to_control(next_settings)
                                ui.notify(
                                    "Saved settings." if changed else "Settings are unchanged.",
                                    type="positive",
                                )
                                if plate_actions_changed:
                                    ui.navigate.reload()

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
                                        user_plate=ModWebUserPlateSettings(),
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

                                plate_actions_changed = current_settings.user_plate != next_settings.user_plate
                                current_settings = next_settings
                                _apply_color_values_to_controls(
                                    self._resolved_user_appearance_colors(next_settings.appearance)
                                )
                                _apply_country_to_control(next_settings.country)
                                _apply_user_plate_settings_to_control(next_settings)
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
                                if plate_actions_changed:
                                    ui.navigate.reload()

                        with ui.row().classes("w-full justify-end mod-app-details-actions"):
                            ui.button("Reset", on_click=_reset_appearance_colors).classes("mod-list-button secondary")
                            ui.button("Save", on_click=_save_appearance_colors).classes("mod-list-button")
                            ui.button("Close", on_click=settings_dialog.close).classes("mod-list-button secondary")
                settings_dialog.open()

        return _show_user_settings_panel
