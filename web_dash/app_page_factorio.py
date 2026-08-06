from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .nicegui_protocols import ModWebUi, _value_as_bool, _value_as_text
from .runtime_imports import Checkbox, Input, ModWebUser, NodeFactorioGenerationState, Power_Level
from .service_base import ModWebServiceSupport
from .types import ModWebAppTabDefinition, ModWebBasePageModel, _ModWebBadgeSpec
from .ui_helpers import copy_text_to_clipboard

_MAP_GEN_SIZE_NAMES: dict[str, float] = {
    "none": 0.0,
    "very-low": 0.5,
    "very-small": 0.5,
    "very-poor": 0.5,
    "low": 1 / math.sqrt(2),
    "small": 1 / math.sqrt(2),
    "poor": 1 / math.sqrt(2),
    "normal": 1.0,
    "medium": 1.0,
    "regular": 1.0,
    "high": math.sqrt(2),
    "big": math.sqrt(2),
    "good": math.sqrt(2),
    "very-high": 2.0,
    "very-big": 2.0,
    "very-good": 2.0,
}
_CONTROL_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Nauvis",
        (
            ("iron-ore", "Iron ore"),
            ("copper-ore", "Copper ore"),
            ("stone", "Stone"),
            ("coal", "Coal"),
            ("uranium-ore", "Uranium ore"),
            ("crude-oil", "Crude oil"),
            ("water", "Water"),
            ("trees", "Trees"),
            ("enemy-base", "Enemy bases"),
            ("rocks", "Rocks"),
            ("starting_area_moisture", "Starting-area moisture"),
            ("nauvis_cliff", "Cliffs"),
        ),
    ),
    (
        "Vulcanus",
        (
            ("vulcanus_coal", "Coal"),
            ("tungsten_ore", "Tungsten ore"),
            ("calcite", "Calcite"),
            ("sulfuric_acid_geyser", "Sulfuric acid geysers"),
            ("vulcanus_volcanism", "Volcanism"),
        ),
    ),
    (
        "Gleba",
        (
            ("gleba_stone", "Stone"),
            ("gleba_plants", "Yumako and jellynut plants"),
            ("gleba_enemy_base", "Pentapod nests"),
            ("gleba_water", "Water"),
            ("gleba_cliff", "Cliffs"),
        ),
    ),
    (
        "Fulgora",
        (
            ("scrap", "Scrap"),
            ("fulgora_islands", "Islands"),
            ("fulgora_cliff", "Cliffs"),
        ),
    ),
    (
        "Aquilo",
        (
            ("lithium_brine", "Lithium brine"),
            ("fluorine_vent", "Fluorine vents"),
            ("aquilo_crude_oil", "Crude oil"),
        ),
    ),
)
_CONTROL_LABELS: dict[str, str] = {
    control_id: label for _planet, controls in _CONTROL_GROUPS for control_id, label in controls
}
_RESOURCE_CONTROL_IDS = frozenset(
    {
        "iron-ore",
        "copper-ore",
        "stone",
        "coal",
        "uranium-ore",
        "crude-oil",
        "vulcanus_coal",
        "tungsten_ore",
        "calcite",
        "sulfuric_acid_geyser",
        "gleba_stone",
        "scrap",
        "lithium_brine",
        "fluorine_vent",
        "aquilo_crude_oil",
    }
)
_TERRAIN_CONTROL_IDS = frozenset(
    {
        "water",
        "trees",
        "rocks",
        "starting_area_moisture",
        "vulcanus_volcanism",
        "gleba_water",
        "gleba_plants",
        "fulgora_islands",
    }
)
_CLIFF_CONTROL_IDS = frozenset({"nauvis_cliff", "gleba_cliff", "fulgora_cliff"})
_ENEMY_CONTROL_IDS = frozenset({"enemy-base", "gleba_enemy_base"})
_KNOWN_CONTROL_IDS = _RESOURCE_CONTROL_IDS | _TERRAIN_CONTROL_IDS | _CLIFF_CONTROL_IDS | _ENEMY_CONTROL_IDS
_UINT32_MAX = 4_294_967_295
_MAP_DIMENSION_LIMIT = 2_000_000
_FACTORIO_TICKS_PER_MINUTE = 3_600

_FactorioFieldKind = Literal["number", "integer"]
_FactorioDisplayUnit = Literal["native", "minutes"]


@dataclass(frozen=True, slots=True)
class _FactorioAutoplaceInputs:
    enabled: Checkbox
    fields: Mapping[str, Input]


@dataclass(frozen=True, slots=True)
class _FactorioMapSettingNumber:
    section: str
    field: str
    label: str
    kind: _FactorioFieldKind
    default: float
    maximum: float
    step: float
    hint: str | None = None
    display_unit: _FactorioDisplayUnit = "native"


_ENEMY_EXPANSION_SETTINGS: tuple[_FactorioMapSettingNumber, ...] = (
    _FactorioMapSettingNumber(
        "enemy_expansion", "min_expansion_distance", "Minimum expansion distance", "integer", 3, 32, 1
    ),
    _FactorioMapSettingNumber(
        "enemy_expansion", "max_expansion_distance", "Maximum expansion distance", "integer", 5, 64, 1
    ),
    _FactorioMapSettingNumber(
        "enemy_expansion", "evolution_group_size_factor", "Evolution group size factor", "number", 4, 32, 0.1
    ),
    _FactorioMapSettingNumber(
        "enemy_expansion",
        "min_expansion_cooldown",
        "Minimum cooldown",
        "integer",
        14_400,
        345_600,
        60,
        display_unit="minutes",
    ),
    _FactorioMapSettingNumber(
        "enemy_expansion",
        "max_expansion_cooldown",
        "Maximum cooldown",
        "integer",
        216_000,
        691_200,
        60,
        display_unit="minutes",
    ),
)
_EVOLUTION_SETTINGS: tuple[_FactorioMapSettingNumber, ...] = (
    _FactorioMapSettingNumber("enemy_evolution", "time_factor", "Time factor", "number", 0.000004, 0.0001, 0.000001),
    _FactorioMapSettingNumber("enemy_evolution", "destroy_factor", "Destroy factor", "number", 0.002, 0.1, 0.0001),
    _FactorioMapSettingNumber(
        "enemy_evolution", "pollution_factor", "Pollution factor", "number", 0.0000009, 0.0001, 0.0000001
    ),
)
_POLLUTION_SETTINGS: tuple[_FactorioMapSettingNumber, ...] = (
    _FactorioMapSettingNumber("pollution", "ageing", "Absorption modifier", "number", 1, 5, 0.01),
    _FactorioMapSettingNumber(
        "pollution", "enemy_attack_pollution_consumption_modifier", "Attack cost modifier", "number", 1, 5, 0.01
    ),
    _FactorioMapSettingNumber(
        "pollution", "min_pollution_to_damage_trees", "Minimum to damage trees", "integer", 60, 10_000, 1
    ),
    _FactorioMapSettingNumber(
        "pollution", "pollution_restored_per_tree_damage", "Absorbed per damaged tree", "integer", 10, 10_000, 1
    ),
    _FactorioMapSettingNumber(
        "pollution", "diffusion_ratio", "Diffusion ratio", "number", 0.02, 1, 0.001, "0.02 = 2%."
    ),
)
_ADVANCED_SETTINGS: tuple[_FactorioMapSettingNumber, ...] = (
    _FactorioMapSettingNumber(
        "difficulty_settings", "technology_price_multiplier", "Price multiplier", "number", 1, 100, 0.01
    ),
    _FactorioMapSettingNumber("asteroids", "spawning_rate", "Spawning rate", "number", 1, 10, 0.01),
    _FactorioMapSettingNumber("difficulty_settings", "spoil_time_modifier", "Spoiling rate", "number", 1, 10, 0.01),
)


class ModWebAppPageFactorioMixin(ModWebServiceSupport):
    @staticmethod
    def _factorio_generation_tab_badges(
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
        tab: ModWebAppTabDefinition,
    ) -> tuple[_ModWebBadgeSpec, ...]:
        del user, tab
        state = model.factorio_generation
        if state is None:
            return ()
        if state.load_error is not None:
            return (_ModWebBadgeSpec(text="Load error", tone="warn"),)
        if state.map_gen_settings is None:
            return ()
        controls = state.map_gen_settings.get("autoplace_controls")
        control_count = len(controls) if isinstance(controls, Mapping) else 0
        badges: list[_ModWebBadgeSpec] = [
            _ModWebBadgeSpec(text=f"{control_count} overrides", tone="black" if control_count else "grey"),
        ]
        if state.space_age_enabled:
            badges.append(_ModWebBadgeSpec(text="Space Age", tone="purple"))
        return tuple(badges)

    def _render_factorio_generation_section(
        self,
        *,
        ui: ModWebUi,
        model: ModWebBasePageModel,
        user: ModWebUser,
        tab: ModWebAppTabDefinition,
    ) -> None:
        del tab
        if model.app_scope != "factorio":
            raise ValueError("The Generation tab requires a Factorio app.")
        state = model.factorio_generation
        if not self._user_has_level(user, Power_Level.sudo):
            self._render_flat_tab_empty_state(
                ui=ui,
                title="Generation",
                description="Factorio generation settings require Sudo access.",
                detail_text="These settings control the next freshly generated world and can materially change a server run.",
            )
            return
        if state is None:
            self._render_flat_tab_empty_state(
                ui=ui,
                title="Generation",
                description="Generation settings are not available from this node.",
                detail_text="Reload the tab after the node is reachable.",
            )
            return
        if state.load_error is not None or state.map_gen_settings is None or state.map_settings is None:
            self._render_flat_tab_empty_state(
                ui=ui,
                title="Generation",
                description="Factorio map settings could not be loaded.",
                detail_text=state.load_error or "Factorio did not return JSON objects for its generation settings.",
            )
            return

        map_gen_settings = state.map_gen_settings
        map_settings = state.map_settings
        general_inputs: dict[str, object] = {}
        control_inputs: dict[str, _FactorioAutoplaceInputs] = {}
        map_setting_number_inputs: dict[tuple[str, str], tuple[_FactorioMapSettingNumber, Input]] = {}
        map_setting_toggle_inputs: dict[tuple[str, str], Checkbox] = {}

        async def save_generation() -> None:
            try:
                updated_map_gen_settings, updated_map_settings = self._factorio_generation_settings_from_form(
                    initial_map_gen_settings=map_gen_settings,
                    initial_map_settings=map_settings,
                    general_inputs=general_inputs,
                    control_inputs=control_inputs,
                    map_setting_number_inputs=map_setting_number_inputs,
                    map_setting_toggle_inputs=map_setting_toggle_inputs,
                )
                node = self._remote_node_link(model.node_name)
                await self._remote_factorio_generation_update_async(
                    node,
                    model.app_name,
                    updated_map_gen_settings,
                    updated_map_settings,
                    user,
                )
            except Exception as xcp:
                ui.notify(f"Generation settings were not saved: {xcp}", type="negative")
                return
            ui.notify("Generation settings saved for the next fresh world.", type="positive")
            self._guarded_reload(ui=ui)

        async def import_map_exchange_string() -> None:
            raw_value = _value_as_text(map_exchange_input)
            try:
                if not raw_value.strip():
                    raise ValueError("Paste a Factorio map exchange string first.")
                node = self._remote_node_link(model.node_name)
                await self._remote_factorio_map_exchange_import_async(node, model.app_name, raw_value, user)
            except Exception as xcp:
                ui.notify(f"Map exchange import failed: {xcp}", type="negative")
                return
            ui.notify("Map exchange settings imported for the next fresh world.", type="positive")
            self._guarded_reload(ui=ui)

        async def export_map_exchange_string() -> None:
            try:
                node = self._remote_node_link(model.node_name)
                exported = await self._remote_factorio_map_exchange_export_async(node, model.app_name, user)
            except Exception as xcp:
                ui.notify(f"Map exchange export failed: {xcp}", type="negative")
                return
            if copy_text_to_clipboard(
                ui=ui,
                text=exported.map_exchange_string,
                empty_message="Factorio returned an empty map exchange string.",
            ):
                ui.notify("Copied the running world's map exchange string.", type="positive")

        async def sync_running_world_generation() -> None:
            try:
                node = self._remote_node_link(model.node_name)
                await self._remote_factorio_generation_running_world_sync_async(node, model.app_name, user)
            except Exception as xcp:
                ui.notify(f"Running-world generation sync failed: {xcp}", type="negative")
                return
            ui.notify("Generation controls now match the running world.", type="positive")
            self._guarded_reload(ui=ui)

        with ui.card().classes(f"{self._flat_tab_card_classes()} mod-factorio-generator"):
            with ui.element("div").classes("mod-factorio-titlebar"):
                with ui.column().classes("gap-0 min-w-0 grow"):
                    ui.label("World generation").classes("mod-factorio-title")
                    ui.label("New worlds only").classes("mod-factorio-kicker")
                with ui.row().classes("items-center gap-2 flex-wrap mod-factorio-header-actions"):
                    with ui.row().classes("items-center gap-2 flex-wrap mod-factorio-seed"):
                        ui.label("Seed").classes("mod-factorio-seed-label")
                        seed_input = self._factorio_plain_number_input(
                            ui=ui,
                            value=self._factorio_optional_uint(
                                map_gen_settings.get("seed"), field="Seed", maximum=_UINT32_MAX
                            ),
                            label="Seed",
                            maximum=_UINT32_MAX,
                            hint=None,
                            show_label=False,
                        )
                        general_inputs["seed"] = seed_input
                    sync_running_world_button = ui.button(
                        "Load running world", icon="sync", on_click=sync_running_world_generation
                    ).classes("mod-list-button mod-factorio-running-world")
                    if not state.running_world_mapgen_available:
                        sync_running_world_button.disable()
                        with sync_running_world_button:
                            ui.tooltip(
                                "Start Factorio with Yuki Bridge 1.2.0 or newer and wait for its map generation snapshot."
                            )
                    ui.button("Save", icon="save", on_click=save_generation).classes(
                        "mod-list-button mod-factorio-save"
                    )

            with ui.element("div").classes("mod-factorio-tabs-shell"):
                with (
                    ui.tabs()
                    .classes("mod-factorio-tabs")
                    .props("aria-label=Factorio map generator sections") as generator_tabs
                ):
                    resources_tab = ui.tab("Resources", icon="inventory_2")
                    terrain_tab = ui.tab("Terrain", icon="landscape")
                    enemy_tab = ui.tab("Enemy", icon="pest_control")
                    advanced_tab = ui.tab("Advanced", icon="tune")

            with ui.tab_panels(generator_tabs, value=resources_tab, animated=False).classes(
                "mod-factorio-panels w-full bg-transparent"
            ):
                with ui.tab_panel(resources_tab).classes("mod-factorio-panel w-full"):
                    for group_name, controls in self._factorio_controls_for_category(state=state, category="resources"):
                        self._render_factorio_control_table(
                            ui=ui,
                            map_gen_settings=map_gen_settings,
                            title=group_name,
                            controls=controls,
                            columns=(("frequency", "Frequency"), ("size", "Size"), ("richness", "Richness")),
                            control_inputs=control_inputs,
                        )

                with ui.tab_panel(terrain_tab).classes("mod-factorio-panel w-full"):
                    self._render_factorio_terrain_controls(
                        ui=ui,
                        map_gen_settings=map_gen_settings,
                        general_inputs=general_inputs,
                    )
                    self._render_factorio_control_table(
                        ui=ui,
                        map_gen_settings=map_gen_settings,
                        title="Terrain and water",
                        controls=self._factorio_controls_for_category(state=state, category="terrain")[0][1],
                        columns=(("frequency", "Scale"), ("size", "Coverage")),
                        control_inputs=control_inputs,
                    )
                    self._render_factorio_control_table(
                        ui=ui,
                        map_gen_settings=map_gen_settings,
                        title="Cliffs",
                        controls=self._factorio_controls_for_category(state=state, category="cliffs")[0][1],
                        columns=(("frequency", "Frequency"), ("richness", "Continuity")),
                        control_inputs=control_inputs,
                    )

                with ui.tab_panel(enemy_tab).classes("mod-factorio-panel w-full"):
                    self._render_factorio_control_table(
                        ui=ui,
                        map_gen_settings=map_gen_settings,
                        title="Enemy bases",
                        controls=self._factorio_controls_for_category(state=state, category="enemy")[0][1],
                        columns=(("frequency", "Frequency"), ("size", "Size")),
                        control_inputs=control_inputs,
                    )
                    with ui.element("div").classes("mod-factorio-option-group mod-factorio-starting-conditions"):
                        with ui.element("div").classes("mod-factorio-option-grid"):
                            starting_area = self._render_factorio_range_input(
                                ui=ui,
                                label="Starting area size",
                                value=self._factorio_map_gen_size(map_gen_settings.get("starting_area")),
                                default=1,
                                maximum=6,
                                step=0.05,
                            )
                            general_inputs["starting_area"] = starting_area
                            with ui.column().classes("mod-factorio-toggle-row"):
                                no_enemies = ui.switch(
                                    "No enemies", value=map_gen_settings.get("no_enemies_mode") is True
                                )
                                peaceful = ui.switch(
                                    "Peaceful mode", value=map_gen_settings.get("peaceful_mode") is True
                                )
                                general_inputs["no_enemies_mode"] = no_enemies
                                general_inputs["peaceful_mode"] = peaceful
                    self._render_factorio_map_settings_group(
                        ui=ui,
                        map_settings=map_settings,
                        title="Enemy expansion",
                        toggle=("enemy_expansion", "enabled", "Enemy expansion", True),
                        settings=_ENEMY_EXPANSION_SETTINGS,
                        number_inputs=map_setting_number_inputs,
                        toggle_inputs=map_setting_toggle_inputs,
                    )
                    self._render_factorio_map_settings_group(
                        ui=ui,
                        map_settings=map_settings,
                        title="Evolution",
                        toggle=("enemy_evolution", "enabled", "Evolution", True),
                        settings=_EVOLUTION_SETTINGS,
                        number_inputs=map_setting_number_inputs,
                        toggle_inputs=map_setting_toggle_inputs,
                    )

                with ui.tab_panel(advanced_tab).classes("mod-factorio-panel w-full"):
                    with ui.element("div").classes("mod-factorio-advanced-grid mod-factorio-advanced-top-grid"):
                        with ui.element("div").classes("mod-factorio-option-group"):
                            with ui.row().classes("w-full items-baseline justify-between gap-2"):
                                ui.label("Map").classes("mod-factorio-group-title")
                                ui.label("0 = unlimited").classes("mod-factorio-group-hint")
                            width = self._factorio_plain_number_input(
                                ui=ui,
                                value=self._factorio_optional_uint(
                                    map_gen_settings.get("width"), field="Width", maximum=_MAP_DIMENSION_LIMIT
                                ),
                                label="Width",
                                maximum=_MAP_DIMENSION_LIMIT,
                                hint=None,
                            )
                            height = self._factorio_plain_number_input(
                                ui=ui,
                                value=self._factorio_optional_uint(
                                    map_gen_settings.get("height"), field="Height", maximum=_MAP_DIMENSION_LIMIT
                                ),
                                label="Height",
                                maximum=_MAP_DIMENSION_LIMIT,
                                hint=None,
                            )
                            general_inputs["width"] = width
                            general_inputs["height"] = height
                        self._render_factorio_map_settings_group(
                            ui=ui,
                            map_settings=map_settings,
                            title="Technology",
                            toggle=None,
                            settings=_ADVANCED_SETTINGS[:1],
                            number_inputs=map_setting_number_inputs,
                            toggle_inputs=map_setting_toggle_inputs,
                        )
                    self._render_factorio_map_settings_group(
                        ui=ui,
                        map_settings=map_settings,
                        title="Pollution",
                        toggle=("pollution", "enabled", "Pollution", True),
                        settings=_POLLUTION_SETTINGS,
                        number_inputs=map_setting_number_inputs,
                        toggle_inputs=map_setting_toggle_inputs,
                    )
                    with ui.element("div").classes("mod-factorio-advanced-grid"):
                        self._render_factorio_map_settings_group(
                            ui=ui,
                            map_settings=map_settings,
                            title="Asteroids",
                            toggle=None,
                            settings=_ADVANCED_SETTINGS[1:2],
                            number_inputs=map_setting_number_inputs,
                            toggle_inputs=map_setting_toggle_inputs,
                        )
                        self._render_factorio_map_settings_group(
                            ui=ui,
                            map_settings=map_settings,
                            title="Spoiling",
                            toggle=None,
                            settings=_ADVANCED_SETTINGS[2:],
                            number_inputs=map_setting_number_inputs,
                            toggle_inputs=map_setting_toggle_inputs,
                        )

            with ui.element("div").classes("mod-factorio-map-string"):
                with ui.column().classes("gap-1 min-w-0 grow"):
                    ui.label("Map exchange string").classes("mod-factorio-group-title")
                map_exchange_input = (
                    ui.textarea(placeholder=">>>…<<<")
                    .props('aria-label="Map exchange string" rows=3 filled square dense hide-bottom-space')
                    .classes("mod-factorio-map-string-input grow")
                )
                with ui.row().classes("gap-2 flex-wrap"):
                    import_button = ui.button("Import", icon="input", on_click=import_map_exchange_string).classes(
                        "mod-list-button mod-factorio-map-string-action"
                    )
                    export_button = ui.button(
                        "Copy current", icon="content_copy", on_click=export_map_exchange_string
                    ).classes("mod-list-button mod-factorio-map-string-action")
                    if not state.map_exchange_available:
                        import_button.disable()
                        export_button.disable()

    def _render_factorio_control_table(
        self,
        *,
        ui: ModWebUi,
        map_gen_settings: Mapping[str, object],
        title: str,
        controls: tuple[tuple[str, str], ...],
        columns: tuple[tuple[str, str], ...],
        control_inputs: dict[str, _FactorioAutoplaceInputs],
    ) -> None:
        if not controls:
            return
        with ui.element("div").classes(f"mod-factorio-control-table mod-factorio-control-table-cols-{len(columns)}"):
            ui.label(title).classes("mod-factorio-group-title")
            with ui.element("div").classes("mod-factorio-control-header"):
                ui.label("Resource / feature")
                for _field, label in columns:
                    ui.label(label)
            for control_id, control_label in controls:
                existing_values = self._factorio_control_field_values(map_gen_settings, control_id)
                enabled = self._factorio_control_is_enabled(existing_values)
                with ui.element("div").classes("mod-factorio-control-row"):
                    with ui.row().classes("items-center gap-2 min-w-0"):
                        checkbox = (
                            ui.checkbox(value=enabled)
                            .props(f'aria-label="Enable {control_label}"')
                            .classes("mod-factorio-control-enabled")
                        )
                        ui.label(control_label).classes("mod-factorio-control-label")
                    inputs: dict[str, Input] = {}
                    for field, label in columns:
                        inputs[field] = self._render_factorio_range_input(
                            ui=ui,
                            label=label,
                            value=existing_values[field],
                            default=1,
                            maximum=6,
                            step=0.05,
                            enabled_from=checkbox,
                        )
                    control_inputs[control_id] = _FactorioAutoplaceInputs(enabled=checkbox, fields=inputs)

    def _render_factorio_terrain_controls(
        self,
        *,
        ui: ModWebUi,
        map_gen_settings: Mapping[str, object],
        general_inputs: dict[str, object],
    ) -> None:
        raw_expressions = map_gen_settings.get("property_expression_names")
        expressions = raw_expressions if isinstance(raw_expressions, Mapping) else {}
        raw_elevation = expressions.get("elevation")
        elevation = raw_elevation.strip() if isinstance(raw_elevation, str) else ""
        map_type_options = {
            "": "Normal terrain",
            "elevation_lakes": "Lakes elevation",
            "elevation_island": "Island",
        }
        if elevation and elevation not in map_type_options:
            map_type_options[elevation] = f"Custom expression: {elevation}"
        with ui.element("div").classes("mod-factorio-option-group"):
            ui.label("Map type").classes("mod-factorio-group-title")
            map_type = (
                ui.select(map_type_options, value=elevation, label="Elevation profile")
                .props("filled square dense hide-bottom-space options-dark")
                .classes("mod-factorio-map-type")
            )
            general_inputs["map_type"] = map_type
            with ui.element("div").classes("mod-factorio-option-grid"):
                for control_name, label in (("moisture", "Moisture"), ("aux", "Terrain type")):
                    scale = self._render_factorio_range_input(
                        ui=ui,
                        label=f"{label} scale",
                        value=self._factorio_expression_scale(expressions.get(f"control:{control_name}:frequency")),
                        default=1,
                        maximum=6,
                        step=0.05,
                        minimum=0.05,
                    )
                    bias = self._render_factorio_range_input(
                        ui=ui,
                        label=f"{label} bias",
                        value=self._factorio_expression_number(expressions.get(f"control:{control_name}:bias")),
                        default=0,
                        maximum=1,
                        step=0.01,
                        minimum=-1,
                    )
                    general_inputs[f"{control_name}_scale"] = scale
                    general_inputs[f"{control_name}_bias"] = bias

    def _render_factorio_map_settings_group(
        self,
        *,
        ui: ModWebUi,
        map_settings: Mapping[str, object],
        title: str,
        toggle: tuple[str, str, str, bool] | None,
        settings: tuple[_FactorioMapSettingNumber, ...],
        number_inputs: dict[tuple[str, str], tuple[_FactorioMapSettingNumber, Input]],
        toggle_inputs: dict[tuple[str, str], Checkbox],
    ) -> None:
        with ui.element("div").classes("mod-factorio-option-group"):
            with ui.row().classes("w-full items-center justify-between gap-3"):
                ui.label(title).classes("mod-factorio-group-title")
                if toggle is not None:
                    section, field, label, default = toggle
                    toggle_input = ui.checkbox(
                        label,
                        value=self._factorio_map_setting_bool(
                            map_settings, section=section, field=field, default=default
                        ),
                    ).classes("mod-factorio-section-toggle")
                    toggle_inputs[(section, field)] = toggle_input
            with ui.element("div").classes("mod-factorio-option-grid"):
                for setting in settings:
                    raw_value = self._factorio_map_setting_number(
                        map_settings,
                        section=setting.section,
                        field=setting.field,
                    )
                    input_control = self._render_factorio_range_input(
                        ui=ui,
                        label=self._factorio_map_setting_display_label(setting),
                        value=self._factorio_map_setting_display_value(setting, raw_value),
                        default=self._factorio_map_setting_display_number(setting, setting.default),
                        maximum=self._factorio_map_setting_display_number(setting, setting.maximum),
                        step=self._factorio_map_setting_display_number(setting, setting.step),
                        hint=setting.hint,
                    )
                    number_inputs[(setting.section, setting.field)] = (setting, input_control)

    def _render_factorio_range_input(
        self,
        *,
        ui: ModWebUi,
        label: str,
        value: float | None,
        default: float,
        maximum: float,
        step: float,
        hint: str | None = None,
        minimum: float = 0,
        enabled_from: Checkbox | None = None,
    ) -> Input:
        displayed_value = default if value is None else min(max(value, minimum), maximum)
        with ui.column().classes("mod-factorio-range-field"):
            ui.label(label).classes("mod-factorio-range-label")
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                input_control = ui.input(value=self._factorio_optional_number_text(value), placeholder="Default")
                input_control.props(
                    f"type=number min={self._factorio_number_text(minimum)} max={self._factorio_number_text(maximum)} "
                    f'step={self._factorio_number_text(step)} aria-label="{label}" filled square dense hide-bottom-space'
                ).classes("mod-factorio-range-value")

                def slider_changed(event: object, *, target: Input = input_control) -> None:
                    target.set_value(
                        self._factorio_number_text(
                            self._factorio_slider_value(event, default=default, minimum=minimum, maximum=maximum)
                        )
                    )

                slider = (
                    ui.slider(min=minimum, max=maximum, step=step, value=displayed_value, on_change=slider_changed)
                    .props(f'aria-label="{label}" color=warning')
                    .classes("mod-factorio-slider grow")
                )

                def input_entered() -> None:
                    input_text = _value_as_text(input_control).strip()
                    slider_value = self._factorio_slider_value(
                        input_control,
                        default=default,
                        minimum=minimum,
                        maximum=maximum,
                    )
                    slider.set_value(slider_value)
                    if input_text:
                        input_control.set_value(self._factorio_number_text(slider_value))

                input_control.on("keydown.enter", input_entered)
                if enabled_from is not None:
                    input_control.bind_enabled_from(enabled_from, "value")
                    slider.bind_enabled_from(enabled_from, "value")
            if hint is not None:
                ui.label(hint).classes("mod-factorio-range-hint")
        return input_control

    def _factorio_plain_number_input(
        self,
        *,
        ui: ModWebUi,
        value: int | None,
        label: str,
        maximum: int,
        hint: str | None,
        show_label: bool = True,
    ) -> Input:
        with ui.column().classes("mod-factorio-plain-number"):
            input_control = ui.input(
                label=label if show_label else None,
                value="" if value is None else str(value),
                placeholder="Random" if label == "Seed" else "0",
            )
            input_control.props(
                f'type=number min=0 max={maximum:,} step=1 aria-label="{label}" filled square dense hide-bottom-space'
            ).classes("mod-factorio-plain-value")
            if hint is not None:
                ui.label(hint).classes("mod-factorio-range-hint")
        return input_control

    @classmethod
    def _factorio_controls_for_category(
        cls,
        *,
        state: NodeFactorioGenerationState,
        category: Literal["resources", "terrain", "cliffs", "enemy"],
    ) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
        if state.map_gen_settings is None:
            return ()
        category_ids = {
            "resources": _RESOURCE_CONTROL_IDS,
            "terrain": _TERRAIN_CONTROL_IDS,
            "cliffs": _CLIFF_CONTROL_IDS,
            "enemy": _ENEMY_CONTROL_IDS,
        }[category]
        raw_controls = state.map_gen_settings.get("autoplace_controls")
        configured_ids = (
            {control_id for control_id in raw_controls if isinstance(control_id, str)}
            if isinstance(raw_controls, Mapping)
            else set[str]()
        )
        available_ids = configured_ids | {control_id for control_id, _label in _CONTROL_GROUPS[0][1]}
        if state.space_age_enabled:
            for _group_name, controls in _CONTROL_GROUPS[1:]:
                available_ids.update(control_id for control_id, _label in controls)
        groups: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        for group_name, controls in _CONTROL_GROUPS:
            selected = tuple(
                (control_id, label)
                for control_id, label in controls
                if control_id in category_ids and control_id in available_ids
            )
            if selected:
                groups.append((group_name, selected))
        if category == "resources":
            extra_ids = configured_ids - _KNOWN_CONTROL_IDS
            if extra_ids:
                groups.append(
                    (
                        "Other controls",
                        tuple(
                            (control_id, cls._factorio_control_label(control_id)) for control_id in sorted(extra_ids)
                        ),
                    )
                )
        return tuple(groups)

    @staticmethod
    def _factorio_control_label(control_id: str) -> str:
        return _CONTROL_LABELS.get(control_id, control_id.replace("_", " ").replace("-", " ").title())

    @classmethod
    def _factorio_control_field_values(
        cls,
        map_gen_settings: Mapping[str, object],
        control_id: str,
    ) -> dict[str, float | None]:
        raw_controls = map_gen_settings.get("autoplace_controls")
        raw_control = raw_controls.get(control_id) if isinstance(raw_controls, Mapping) else None
        return {
            field: cls._factorio_map_gen_size(raw_control.get(field)) if isinstance(raw_control, Mapping) else None
            for field in ("frequency", "size", "richness")
        }

    @staticmethod
    def _factorio_control_is_enabled(values: Mapping[str, float | None]) -> bool:
        configured_values = tuple(value for value in values.values() if value is not None)
        return not configured_values or any(value > 0 for value in configured_values)

    @classmethod
    def _factorio_map_setting_number(
        cls,
        map_settings: Mapping[str, object],
        *,
        section: str,
        field: str,
    ) -> float | None:
        raw_section = map_settings.get(section)
        if not isinstance(raw_section, Mapping):
            return None
        return cls._factorio_map_gen_size(raw_section.get(field))

    @staticmethod
    def _factorio_map_setting_bool(
        map_settings: Mapping[str, object],
        *,
        section: str,
        field: str,
        default: bool,
    ) -> bool:
        raw_section = map_settings.get(section)
        if not isinstance(raw_section, Mapping):
            return default
        raw_value = raw_section.get(field)
        return raw_value if isinstance(raw_value, bool) else default

    @staticmethod
    def _factorio_expression_number(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None

    @classmethod
    def _factorio_expression_scale(cls, value: object) -> float | None:
        frequency = cls._factorio_expression_number(value)
        if frequency is None or frequency <= 0:
            return None
        return 1 / frequency

    @classmethod
    def _factorio_generation_settings_from_form(
        cls,
        *,
        initial_map_gen_settings: Mapping[str, object],
        initial_map_settings: Mapping[str, object],
        general_inputs: Mapping[str, object],
        control_inputs: Mapping[str, _FactorioAutoplaceInputs],
        map_setting_number_inputs: Mapping[tuple[str, str], tuple[_FactorioMapSettingNumber, Input]],
        map_setting_toggle_inputs: Mapping[tuple[str, str], Checkbox],
    ) -> tuple[dict[str, object], dict[str, object]]:
        map_gen_settings = dict(initial_map_gen_settings)
        seed = cls._factorio_uint_input(general_inputs["seed"], field="Seed", maximum=_UINT32_MAX)
        if seed is None:
            map_gen_settings.pop("seed", None)
        else:
            map_gen_settings["seed"] = seed
        for field in ("width", "height"):
            map_gen_settings[field] = (
                cls._factorio_uint_input(
                    general_inputs[field],
                    field=field.replace("_", " ").title(),
                    maximum=_MAP_DIMENSION_LIMIT,
                )
                or 0
            )
        starting_area = cls._factorio_size_input(general_inputs["starting_area"], field="Starting area")
        if starting_area is None:
            map_gen_settings.pop("starting_area", None)
        else:
            map_gen_settings["starting_area"] = starting_area
        map_gen_settings["peaceful_mode"] = _value_as_bool(general_inputs["peaceful_mode"])
        map_gen_settings["no_enemies_mode"] = _value_as_bool(general_inputs["no_enemies_mode"])
        existing_expressions = map_gen_settings.get("property_expression_names")
        expressions: dict[str, object] = dict(existing_expressions) if isinstance(existing_expressions, Mapping) else {}
        map_type = _value_as_text(general_inputs["map_type"]).strip()
        if map_type:
            expressions["elevation"] = map_type
        else:
            expressions.pop("elevation", None)
        for control_name in ("moisture", "aux"):
            scale = cls._factorio_size_input(
                general_inputs[f"{control_name}_scale"], field=f"{control_name.title()} scale"
            )
            scale_field = f"control:{control_name}:frequency"
            if scale is None:
                expressions.pop(scale_field, None)
            elif scale <= 0:
                raise ValueError(f"{control_name.title()} scale must be greater than zero.")
            else:
                expressions[scale_field] = cls._factorio_number_text(1 / scale)
            bias = cls._factorio_finite_number_input(
                general_inputs[f"{control_name}_bias"], field=f"{control_name.title()} bias"
            )
            bias_field = f"control:{control_name}:bias"
            if bias is None:
                expressions.pop(bias_field, None)
            else:
                expressions[bias_field] = cls._factorio_number_text(bias)
        if expressions:
            map_gen_settings["property_expression_names"] = expressions
        else:
            map_gen_settings.pop("property_expression_names", None)

        existing_controls = map_gen_settings.get("autoplace_controls")
        controls: dict[str, object] = dict(existing_controls) if isinstance(existing_controls, Mapping) else {}
        for control_id, inputs in control_inputs.items():
            raw_control = controls.get(control_id)
            control = dict(raw_control) if isinstance(raw_control, Mapping) else {}
            if not _value_as_bool(inputs.enabled):
                for field in inputs.fields:
                    control[field] = 0.0
            else:
                for field, input_control in inputs.fields.items():
                    value = cls._factorio_size_input(
                        input_control, field=f"{cls._factorio_control_label(control_id)} {field}"
                    )
                    if value is None:
                        control.pop(field, None)
                    else:
                        control[field] = value
            if control:
                controls[control_id] = control
            else:
                controls.pop(control_id, None)
        map_gen_settings["autoplace_controls"] = controls

        map_settings = dict(initial_map_settings)
        mutable_sections: dict[str, dict[str, object]] = {}

        def section_for(section_name: str) -> dict[str, object]:
            section = mutable_sections.get(section_name)
            if section is None:
                raw_section = map_settings.get(section_name)
                section = dict(raw_section) if isinstance(raw_section, Mapping) else {}
                mutable_sections[section_name] = section
            return section

        for (section_name, field), (specification, input_control) in map_setting_number_inputs.items():
            section = section_for(section_name)
            value = cls._factorio_map_setting_input_value(specification, input_control)
            if value is None:
                section.pop(field, None)
            else:
                section[field] = value
        for (section_name, field), input_control in map_setting_toggle_inputs.items():
            section_for(section_name)[field] = _value_as_bool(input_control)
        for section_name, section in mutable_sections.items():
            if section:
                map_settings[section_name] = section
            else:
                map_settings.pop(section_name, None)
        return map_gen_settings, map_settings

    @staticmethod
    def _factorio_map_setting_display_label(specification: _FactorioMapSettingNumber) -> str:
        if specification.display_unit == "minutes":
            return f"{specification.label} (minutes)"
        return specification.label

    @staticmethod
    def _factorio_map_setting_display_value(
        specification: _FactorioMapSettingNumber, value: float | None
    ) -> float | None:
        if value is None:
            return value
        return ModWebAppPageFactorioMixin._factorio_map_setting_display_number(specification, value)

    @staticmethod
    def _factorio_map_setting_display_number(specification: _FactorioMapSettingNumber, value: float) -> float:
        if specification.display_unit == "minutes":
            return value / _FACTORIO_TICKS_PER_MINUTE
        return value

    @classmethod
    def _factorio_map_setting_input_value(
        cls, specification: _FactorioMapSettingNumber, input_control: object
    ) -> float | int | None:
        display_label = cls._factorio_map_setting_display_label(specification)
        if specification.display_unit == "minutes":
            minutes = cls._factorio_size_input(input_control, field=display_label)
            if minutes is None:
                return None
            ticks = minutes * _FACTORIO_TICKS_PER_MINUTE
            rounded_ticks = round(ticks)
            if not math.isclose(ticks, rounded_ticks, rel_tol=0, abs_tol=1e-6):
                raise ValueError(f"{display_label} must resolve to a whole number of Factorio ticks.")
            return rounded_ticks
        if specification.kind == "integer":
            return cls._factorio_uint_input(input_control, field=display_label, maximum=_UINT32_MAX)
        return cls._factorio_size_input(input_control, field=display_label)

    @staticmethod
    def _factorio_optional_uint(value: object, *, field: str, maximum: int) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError(f"{field} must be an integer between 0 and {maximum:,}.")
        return value

    @staticmethod
    def _factorio_map_gen_size(value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("Map generation size values must be numeric.")
        if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
            return float(value)
        if isinstance(value, str):
            named_value = _MAP_GEN_SIZE_NAMES.get(value.strip().casefold())
            if named_value is not None:
                return named_value
            try:
                parsed = float(value)
            except ValueError as xcp:
                raise ValueError(f"Unsupported map generation size: {value!r}") from xcp
            if math.isfinite(parsed) and parsed >= 0:
                return parsed
        raise ValueError("Map generation size values must be non-negative numbers.")

    @classmethod
    def _factorio_uint_input(cls, input_control: object, *, field: str, maximum: int) -> int | None:
        text = _value_as_text(input_control).strip()
        if not text:
            return None
        try:
            parsed = int(text)
        except ValueError as xcp:
            raise ValueError(f"{field} must be a whole number.") from xcp
        if str(parsed) != text and text != f"+{parsed}":
            raise ValueError(f"{field} must be a whole number.")
        return cls._factorio_optional_uint(parsed, field=field, maximum=maximum)

    @classmethod
    def _factorio_size_input(cls, input_control: object, *, field: str) -> float | None:
        text = _value_as_text(input_control).strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError as xcp:
            raise ValueError(f"{field} must be a non-negative number.") from xcp
        return cls._factorio_map_gen_size(parsed)

    @staticmethod
    def _factorio_finite_number_input(input_control: object, *, field: str) -> float | None:
        text = _value_as_text(input_control).strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError as xcp:
            raise ValueError(f"{field} must be a finite number.") from xcp
        if not math.isfinite(parsed):
            raise ValueError(f"{field} must be a finite number.")
        return parsed

    @classmethod
    def _factorio_slider_value(cls, event: object, *, default: float, minimum: float, maximum: float) -> float:
        try:
            value = cls._factorio_map_gen_size(_value_as_text(event))
        except ValueError:
            return default
        if value is None:
            return default
        return min(max(value, minimum), maximum)

    @staticmethod
    def _factorio_number_text(value: float | int) -> str:
        return format(value, ".12g")

    @classmethod
    def _factorio_optional_number_text(cls, value: float | None) -> str:
        return "" if value is None else cls._factorio_number_text(value)
