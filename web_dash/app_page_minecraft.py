from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from apps.minecraft import (
    MinecraftCookingRecipe,
    MinecraftRecipeBook,
    MinecraftRecipeIngredient,
    MinecraftRecipeItemStack,
    MinecraftRecipeKind,
    MinecraftRecipeMutation,
    MinecraftRecipeRemoval,
    MinecraftRecipeRemovalFilter,
    MinecraftShapedRecipe,
    MinecraftShapelessRecipe,
    MinecraftStonecuttingRecipe,
    generated_minecraft_recipe_id,
    generated_minecraft_recipe_mutation_id,
    minecraft_recipe_mutation_id,
    minecraft_recipe_mutation_with_id,
)

from .nicegui_protocols import (
    ModWebEventArgumentsContainer,
    ModWebUi,
    _event_args_as_text,
    _value_as_object,
)
from .constants import _SEARCH_INPUT_DEBOUNCE_MILLISECONDS
from .runtime_imports import (
    Callable,
    ModWebUser,
    Power_Level,
    config,
    escape,
    json,
    urlencode,
)
from .service_base import ModWebServiceSupport
from .types import (
    ModWebAppTabDefinition,
    ModWebBasePageModel,
    ModWebMinecraftItemRegistrySummary,
    ModWebMinecraftRecipeBookSummary,
    ModWebMinecraftRecipeEntry,
    ModWebMinecraftRecipeOperationKind,
    ModWebPageModel,
    _ModWebBadgeSpec,
)


class _MinecraftRecipeEditorArea(Enum):
    OUTPUT = "output"
    SHAPELESS = "shapeless"
    SHAPED = "shaped"
    COOKING_INPUT = "cooking_input"
    STONECUTTING_INPUT = "stonecutting_input"
    REMOVAL_OUTPUT = "removal_output"
    REMOVAL_INPUT = "removal_input"


class _MinecraftRecipeEditorOperation(Enum):
    ADD = "add"
    REMOVE = "remove"


class _MinecraftRecipeEditorIngredientKind(Enum):
    ITEM = "item"
    TAG = "tag"


class _MinecraftRecipeBrowserItemType(Enum):
    ALL = "all"
    ITEM = "item"
    BLOCK = "block"


@dataclass(slots=True)
class _MinecraftRecipeEditorSelection:
    area: _MinecraftRecipeEditorArea
    index: int | None = None

    def __post_init__(self) -> None:
        if self.area in {_MinecraftRecipeEditorArea.SHAPELESS, _MinecraftRecipeEditorArea.SHAPED}:
            if self.index is None or not 0 <= self.index < 9:
                raise ValueError("Grid recipe selections require an index from 0 to 8.")
            return
        if self.index is not None:
            raise ValueError("Non-grid recipe selections must not define an index.")

    @classmethod
    def output(cls) -> "_MinecraftRecipeEditorSelection":
        return cls(_MinecraftRecipeEditorArea.OUTPUT)

    @classmethod
    def shapeless(cls, index: int) -> "_MinecraftRecipeEditorSelection":
        return cls(_MinecraftRecipeEditorArea.SHAPELESS, index=index)

    @classmethod
    def shaped(cls, index: int) -> "_MinecraftRecipeEditorSelection":
        return cls(_MinecraftRecipeEditorArea.SHAPED, index=index)

    @classmethod
    def cooking_input(cls) -> "_MinecraftRecipeEditorSelection":
        return cls(_MinecraftRecipeEditorArea.COOKING_INPUT)

    @classmethod
    def stonecutting_input(cls) -> "_MinecraftRecipeEditorSelection":
        return cls(_MinecraftRecipeEditorArea.STONECUTTING_INPUT)

    @classmethod
    def removal_output(cls) -> "_MinecraftRecipeEditorSelection":
        return cls(_MinecraftRecipeEditorArea.REMOVAL_OUTPUT)

    @classmethod
    def removal_input(cls) -> "_MinecraftRecipeEditorSelection":
        return cls(_MinecraftRecipeEditorArea.REMOVAL_INPUT)


@dataclass(slots=True)
class _MinecraftRecipeEditorIngredientState:
    kind: _MinecraftRecipeEditorIngredientKind = _MinecraftRecipeEditorIngredientKind.ITEM
    resource_id: str = ""

    @property
    def has_value(self) -> bool:
        return bool(self.resource_id.strip())

    @property
    def editor_text(self) -> str:
        resource_id = self.resource_id.strip()
        if not resource_id:
            return ""
        if self.kind is _MinecraftRecipeEditorIngredientKind.TAG:
            return f"#{resource_id}"
        return resource_id

    @classmethod
    def empty(cls) -> "_MinecraftRecipeEditorIngredientState":
        return cls()

    @classmethod
    def item(cls, item_id: str) -> "_MinecraftRecipeEditorIngredientState":
        return cls(kind=_MinecraftRecipeEditorIngredientKind.ITEM, resource_id=item_id.strip())

    @classmethod
    def tag(cls, tag_id: str) -> "_MinecraftRecipeEditorIngredientState":
        return cls(kind=_MinecraftRecipeEditorIngredientKind.TAG, resource_id=tag_id.strip())


@dataclass(frozen=True, slots=True)
class _MinecraftRecipeBrowserEntry:
    item_id: str
    display_name: str
    namespace: str
    item_type: _MinecraftRecipeBrowserItemType


@dataclass(frozen=True, slots=True)
class _MinecraftRecipeDragPayload:
    kind: _MinecraftRecipeEditorIngredientKind
    resource_id: str

    def __post_init__(self) -> None:
        resource_id = self.resource_id.strip()
        if not resource_id:
            raise ValueError("Minecraft recipe drag payloads require a resource id.")
        object.__setattr__(self, "resource_id", resource_id)

    def to_mapping(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "resource_id": self.resource_id,
        }

    @classmethod
    def from_mapping(cls, payload: object) -> "_MinecraftRecipeDragPayload":
        if not isinstance(payload, dict):
            raise ValueError("Minecraft recipe drag payload is invalid.")
        raw_kind = payload.get("kind")
        raw_resource_id = payload.get("resource_id")
        if not isinstance(raw_kind, str) or not isinstance(raw_resource_id, str):
            raise ValueError("Minecraft recipe drag payload is invalid.")
        return cls(
            kind=_MinecraftRecipeEditorIngredientKind(raw_kind),
            resource_id=raw_resource_id,
        )


@dataclass(slots=True)
class _MinecraftRecipeEditorState:
    operation: _MinecraftRecipeEditorOperation = _MinecraftRecipeEditorOperation.ADD
    kind: MinecraftRecipeKind = MinecraftRecipeKind.SHAPELESS
    editing_recipe_index: int | None = None
    recipe_id: str = ""
    output_item_id: str = ""
    output_count_text: str = "1"
    shapeless_ingredients: list[_MinecraftRecipeEditorIngredientState] = field(
        default_factory=lambda: [_MinecraftRecipeEditorIngredientState.empty() for _ in range(9)]
    )
    shaped_ingredients: list[_MinecraftRecipeEditorIngredientState] = field(
        default_factory=lambda: [_MinecraftRecipeEditorIngredientState.empty() for _ in range(9)]
    )
    cooking_input_ingredient: _MinecraftRecipeEditorIngredientState = field(
        default_factory=_MinecraftRecipeEditorIngredientState.empty
    )
    cooking_experience_text: str = ""
    cooking_time_ticks_text: str = ""
    stonecutting_input_ingredient: _MinecraftRecipeEditorIngredientState = field(
        default_factory=_MinecraftRecipeEditorIngredientState.empty
    )
    removal_recipe_id: str = ""
    removal_output_filter: _MinecraftRecipeEditorIngredientState = field(
        default_factory=_MinecraftRecipeEditorIngredientState.empty
    )
    removal_input_filter: _MinecraftRecipeEditorIngredientState = field(
        default_factory=_MinecraftRecipeEditorIngredientState.empty
    )
    removal_recipe_type_text: str = ""
    removal_mod_id: str = ""
    search_text: str = ""
    browser_namespace: str = ""
    browser_item_type: _MinecraftRecipeBrowserItemType = _MinecraftRecipeBrowserItemType.ALL
    page_index: int = 0
    selected_slot: _MinecraftRecipeEditorSelection = field(default_factory=_MinecraftRecipeEditorSelection.output)


class ModWebAppPageMinecraftMixin(ModWebServiceSupport):
    def _minecraft_recipes_tab_badges(
        self,
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
        tab: ModWebAppTabDefinition,
    ) -> tuple[_ModWebBadgeSpec, ...]:
        del user, tab
        if not isinstance(model, ModWebPageModel):
            return ()
        enabled_mod_names: tuple[str, ...] = tuple(mod.name for mod in model.mods.mods if mod.enabled)
        addon_labels: tuple[str, ...] = self._kubejs_recipe_addon_labels(enabled_mod_names)
        badges: list[_ModWebBadgeSpec] = [
            _ModWebBadgeSpec(text="KubeJS", tone="purple"),
            _ModWebBadgeSpec(text="Managed script", tone="black"),
        ]
        if addon_labels:
            badges.append(_ModWebBadgeSpec(text=f"{len(addon_labels)} addons", tone="grey"))
        return tuple(badges)

    def _render_minecraft_recipes_section(
        self,
        *,
        ui: ModWebUi,
        model: ModWebBasePageModel,
        user: ModWebUser,
        tab: ModWebAppTabDefinition,
    ) -> None:
        del tab
        if not isinstance(model, ModWebPageModel):
            raise TypeError("The Minecraft Recipes tab requires a full mod page model.")

        enabled_mod_names: tuple[str, ...] = tuple(mod.name for mod in model.mods.mods if mod.enabled)
        addon_labels: tuple[str, ...] = self._kubejs_recipe_addon_labels(enabled_mod_names)
        addon_text: str = ", ".join(addon_labels) if addon_labels else "No supported KubeJS recipe addons detected."
        recipe_summary = model.minecraft_recipes
        item_registry_summary = model.minecraft_item_registry
        data_path = recipe_summary.data_path if recipe_summary is not None else ".yukibot/recipes.json"
        script_path = (
            recipe_summary.script_path if recipe_summary is not None else "kubejs/server_scripts/yuki_recipes.js"
        )
        item_registry_markup = self._minecraft_item_registry_markup(item_registry_summary)
        ui.html(
            (
                '<div class="mod-card mod-card-plain w-full">'
                '<div class="mod-tab-toolbar mod-tab-toolbar-surface">'
                "<div>"
                '<div class="mod-title">Minecraft Recipes</div>'
                '<div class="mod-subtitle">Hidden KubeJS-backed recipe workspace.</div>'
                "</div>"
                "</div>"
                '<div class="mod-config-file-body">'
                '<div class="mod-subtitle">'
                "YukiBot stores recipe data in "
                f"<code>{escape(data_path)}</code> and writes generated KubeJS to "
                f"<code>{escape(script_path)}</code>."
                "</div>"
                '<div class="mod-subtitle">'
                f"Supported addons: {escape(addon_text)}"
                "</div>"
                f"{item_registry_markup}"
                "</div>"
                "</div>"
            )
        ).classes("w-full")
        editor_state = _MinecraftRecipeEditorState()

        def edit_managed_recipe(mutation_index: int, mutation: MinecraftRecipeMutation) -> None:
            self._load_minecraft_recipe_editor_state(
                editor_state,
                mutation,
                mutation_index=mutation_index,
            )
            refresh_editor_workspace()
            recipe_subtabs.set_value(add_tab)

        with ui.element("div").classes("mod-recipe-subtabs w-full"):
            with ui.element("div").classes("mod-section-tabs-shell"):
                with ui.tabs().classes("mod-section-tabs") as recipe_subtabs:
                    add_tab = ui.tab("Add")
                    manage_tab = ui.tab("Manage")
            with ui.tab_panels(
                recipe_subtabs,
                value=add_tab,
                animated=False,
            ).classes("mod-section-panels mod-recipe-subtab-panels w-full bg-transparent"):
                with ui.tab_panel(add_tab).classes("mod-section-panel w-full"):
                    refresh_editor_workspace = self._render_minecraft_recipe_add_form(
                        ui=ui,
                        model=model,
                        user=user,
                        editor_state=editor_state,
                    )
                with ui.tab_panel(manage_tab).classes("mod-section-panel w-full"):
                    self._render_minecraft_recipe_manage_panel(
                        ui=ui,
                        model=model,
                        user=user,
                        editor_state=editor_state,
                        on_edit=edit_managed_recipe,
                    )

    def _render_minecraft_recipe_add_form(
        self,
        *,
        ui: ModWebUi,
        model: ModWebPageModel,
        user: ModWebUser,
        editor_state: _MinecraftRecipeEditorState,
    ) -> Callable[[], None]:
        can_write = self._user_has_level(user, Power_Level.sudo)
        if not can_write:
            ui.html(
                (
                    '<div class="mod-card mod-card-empty mod-card-plain">'
                    '<div class="mod-subtitle">Recipe editing is read-only for non-sudo accounts.</div>'
                    "</div>"
                )
            ).classes("w-full")
            return lambda: None

        known_item_ids = self._minecraft_known_item_ids(model.minecraft_item_registry)
        block_item_ids = (
            () if model.minecraft_item_registry is None else model.minecraft_item_registry.block_item_ids
        )
        item_types_classified = bool(
            model.minecraft_item_registry is not None
            and model.minecraft_item_registry.item_types_classified
        )
        browser_entries: tuple[_MinecraftRecipeBrowserEntry, ...] = self._minecraft_browser_entries(
            known_item_ids,
            block_item_ids=block_item_ids,
        )
        browser_namespaces = tuple(sorted({entry.namespace for entry in browser_entries}))
        item_icon_api_url = model.minecraft_item_icon_api_url
        minecraft_username = config.Name_Cache().get_game_alias(user.discord_id, "minecraft")
        managed_mutations = self._minecraft_recipe_mutations(model.minecraft_recipes)
        existing_recipe_ids = {
            recipe_id
            for mutation in managed_mutations
            if (recipe_id := minecraft_recipe_mutation_id(mutation)) is not None
        }
        page_size = 60
        operation_options: dict[str, str] = {
            _MinecraftRecipeEditorOperation.ADD.value: "Add Recipe",
            _MinecraftRecipeEditorOperation.REMOVE.value: "Remove Recipe",
        }
        kind_options: dict[str, str] = {
            MinecraftRecipeKind.SHAPELESS.value: "Shapeless",
            MinecraftRecipeKind.SHAPED.value: "Shaped",
            MinecraftRecipeKind.SMELTING.value: "Smelting",
            MinecraftRecipeKind.BLASTING.value: "Blasting",
            MinecraftRecipeKind.SMOKING.value: "Smoking",
            MinecraftRecipeKind.CAMPFIRE_COOKING.value: "Campfire Cooking",
            MinecraftRecipeKind.STONECUTTING.value: "Stonecutting",
        }
        namespace_options: dict[str, str] = {
            "": "All Mods",
            **{
                namespace: self._minecraft_namespace_display_name(namespace)
                for namespace in browser_namespaces
            },
        }
        item_type_options: dict[str, str] = {
            _MinecraftRecipeBrowserItemType.ALL.value: "All Types",
            _MinecraftRecipeBrowserItemType.ITEM.value: "Items",
            _MinecraftRecipeBrowserItemType.BLOCK.value: "Blocks",
        }

        def refresh_all() -> None:
            render_editor.refresh()
            render_browser_summary.refresh()
            render_browser_grid.refresh()

        def other_recipe_ids() -> set[str]:
            recipe_ids = set(existing_recipe_ids)
            mutation_index = editor_state.editing_recipe_index
            if mutation_index is not None and mutation_index < len(managed_mutations):
                current_recipe_id = minecraft_recipe_mutation_id(managed_mutations[mutation_index])
                if current_recipe_id is not None:
                    recipe_ids.discard(current_recipe_id)
            return recipe_ids

        def set_operation(operation_value: str) -> None:
            editor_state.operation = _MinecraftRecipeEditorOperation(operation_value)
            self._sync_minecraft_recipe_selection(editor_state)
            refresh_all()

        def set_kind(kind_value: str) -> None:
            editor_state.kind = MinecraftRecipeKind(kind_value)
            self._sync_minecraft_recipe_selection(editor_state)
            refresh_all()

        def set_removal_recipe_id(value: str) -> None:
            editor_state.removal_recipe_id = value.strip()

        def set_output_count(raw_value: str) -> None:
            editor_state.output_count_text = raw_value.strip()

        def set_cooking_experience(value: str) -> None:
            editor_state.cooking_experience_text = value.strip()

        def set_cooking_time_ticks(value: str) -> None:
            editor_state.cooking_time_ticks_text = value.strip()

        def set_removal_output_filter(value: str) -> None:
            parsed = self._parse_editor_ingredient_state_text(value)
            editor_state.removal_output_filter.kind = parsed.kind
            editor_state.removal_output_filter.resource_id = parsed.resource_id

        def set_removal_input_filter(value: str) -> None:
            parsed = self._parse_editor_ingredient_state_text(value)
            editor_state.removal_input_filter.kind = parsed.kind
            editor_state.removal_input_filter.resource_id = parsed.resource_id

        def set_removal_recipe_type(value: str) -> None:
            editor_state.removal_recipe_type_text = value.strip()

        def set_removal_mod_id(value: str) -> None:
            editor_state.removal_mod_id = value.strip()

        def set_selected_ingredient_kind(kind_value: str) -> None:
            ingredient_state = self._minecraft_selected_ingredient_state(editor_state)
            if ingredient_state is None:
                return
            ingredient_state.kind = _MinecraftRecipeEditorIngredientKind(kind_value)
            render_editor.refresh()

        def set_selected_ingredient_resource(value: str) -> None:
            ingredient_state = self._minecraft_selected_ingredient_state(editor_state)
            if ingredient_state is None:
                return
            text = value.strip()
            if text.startswith("#"):
                text = text[1:].strip()
            ingredient_state.resource_id = text

        def select_slot(selection: _MinecraftRecipeEditorSelection) -> None:
            editor_state.selected_slot = selection
            render_editor.refresh()

        def apply_item_to_selected_slot(item_id: str) -> None:
            self._apply_minecraft_recipe_item_to_selection(editor_state, item_id)
            render_editor.refresh()

        def apply_drag_payload_to_slot(selection: _MinecraftRecipeEditorSelection, payload: object) -> None:
            try:
                drag_payload = _MinecraftRecipeDragPayload.from_mapping(payload)
            except ValueError:
                return
            try:
                self._apply_minecraft_recipe_drag_payload_to_selection(
                    editor_state,
                    selection=selection,
                    payload=drag_payload,
                )
            except ValueError as xcp:
                ui.notify(str(xcp), type="warning")
                return
            editor_state.selected_slot = selection
            render_editor.refresh()

        def clear_selected_slot() -> None:
            self._clear_minecraft_recipe_selection(editor_state)
            render_editor.refresh()

        def clear_recipe() -> None:
            self._reset_minecraft_recipe_editor(editor_state)
            render_editor.refresh()
            render_browser_summary.refresh()

        def change_search_text(value: str) -> None:
            editor_state.search_text = value.strip()
            editor_state.page_index = 0
            render_browser_summary.refresh()
            render_browser_grid.refresh()

        def change_browser_namespace(value: str) -> None:
            editor_state.browser_namespace = value.strip().casefold()
            editor_state.page_index = 0
            render_browser_summary.refresh()
            render_browser_grid.refresh()

        def change_browser_item_type(value: str) -> None:
            editor_state.browser_item_type = _MinecraftRecipeBrowserItemType(value)
            editor_state.page_index = 0
            render_browser_summary.refresh()
            render_browser_grid.refresh()

        def change_page(delta: int) -> None:
            filtered_entries = self._filtered_minecraft_browser_entries(
                browser_entries,
                editor_state.search_text,
                namespace=editor_state.browser_namespace,
                item_type=editor_state.browser_item_type,
            )
            page_count = self._minecraft_browser_page_count(filtered_entries, page_size=page_size)
            editor_state.page_index = max(0, min(editor_state.page_index + delta, page_count - 1))
            render_browser_summary.refresh()
            render_browser_grid.refresh()

        async def add_recipe() -> None:
            try:
                mutation = self._minecraft_recipe_mutation_from_editor(editor_state)
                if minecraft_username is None:
                    raise ValueError(
                        "Link a Minecraft username to your Discord account before creating recipes or removal "
                        "directives."
                    )
                recipe_id = generated_minecraft_recipe_mutation_id(
                    minecraft_username=minecraft_username,
                    mutation=mutation,
                    existing_recipe_ids=other_recipe_ids(),
                )
                mutation = minecraft_recipe_mutation_with_id(mutation, recipe_id)
                if editor_state.editing_recipe_index is None:
                    await self._append_minecraft_recipe_mutation(model=model, mutation=mutation, user=user)
                    success_message = "Recipe added."
                else:
                    await self._replace_minecraft_recipe_mutation(
                        model=model,
                        mutation_index=editor_state.editing_recipe_index,
                        mutation=mutation,
                        user=user,
                    )
                    success_message = "Recipe updated."
            except Exception as xcp:
                ui.notify(f"Recipe add failed: {xcp}", type="negative")
                return
            ui.notify(success_message, type="positive")
            ui.navigate.reload()

        @ui.refreshable
        def render_editor() -> None:
            with ui.card().classes("mod-card mod-card-plain mod-recipe-editor-shell w-full"):
                with ui.column().classes("w-full gap-3"):
                    with ui.element("div").classes("mod-recipe-panel-heading"):
                        ui.label("Recipe Editor").classes("text-sm font-black mod-title-small")
                        if editor_state.editing_recipe_index is None:
                            ui.label("Creating a managed recipe entry.").classes("mod-subtitle")
                        else:
                            ui.label(f"Editing managed recipe #{editor_state.editing_recipe_index + 1}.").classes(
                                "mod-subtitle"
                            )
                        if minecraft_username is None:
                            ui.label(
                                "Link a Minecraft username to create recipes or removal directives."
                            ).classes("mod-subtitle")
                    with ui.element("div").classes("grid grid-cols-1 md:grid-cols-3 gap-3 w-full"):
                        (
                            ui.select(
                                operation_options,
                                value=editor_state.operation.value,
                                label="Action",
                                on_change=lambda event: set_operation(_event_args_as_text(event)),
                            )
                            .props(
                                "filled square dense hide-bottom-space color=accent "
                                "options-dark popup-content-class=mod-setting-menu"
                            )
                            .classes("w-full mod-config-select mod-recipe-field")
                        )
                        if editor_state.operation is _MinecraftRecipeEditorOperation.ADD:
                            (
                                ui.select(
                                    kind_options,
                                    value=editor_state.kind.value,
                                    label="Type",
                                    on_change=lambda event: set_kind(_event_args_as_text(event)),
                                )
                                .props(
                                    "filled square dense hide-bottom-space color=accent "
                                    "options-dark popup-content-class=mod-setting-menu"
                                )
                                .classes("w-full mod-config-select mod-recipe-field")
                            )
                        else:
                            (
                                ui.input(
                                    "Remove Recipe ID",
                                    value=editor_state.removal_recipe_id,
                                    on_change=lambda event: set_removal_recipe_id(_event_args_as_text(event)),
                                )
                                .props("filled square dense hide-bottom-space color=accent")
                                .classes("w-full mod-recipe-field")
                            )
                        if editor_state.operation is _MinecraftRecipeEditorOperation.ADD:
                            displayed_recipe_id = ""
                            if minecraft_username and editor_state.output_item_id:
                                displayed_recipe_id = generated_minecraft_recipe_id(
                                    minecraft_username=minecraft_username,
                                    output_item_id=editor_state.output_item_id,
                                    existing_recipe_ids=other_recipe_ids(),
                                )
                            (
                                ui.input(
                                    "Generated Recipe ID",
                                    value=displayed_recipe_id,
                                )
                                .props("filled square dense readonly hide-bottom-space color=accent")
                                .classes("w-full mod-recipe-field")
                            )
                        else:
                            (
                                ui.input(
                                    "Recipe Type",
                                    value=editor_state.removal_recipe_type_text,
                                    on_change=lambda event: set_removal_recipe_type(_event_args_as_text(event)),
                                )
                                .props("filled square dense hide-bottom-space color=accent")
                                .classes("w-full mod-recipe-field")
                            )
                            displayed_directive_id = ""
                            if minecraft_username is not None:
                                try:
                                    preview_removal = self._minecraft_recipe_mutation_from_editor(editor_state)
                                    displayed_directive_id = generated_minecraft_recipe_mutation_id(
                                        minecraft_username=minecraft_username,
                                        mutation=preview_removal,
                                        existing_recipe_ids=other_recipe_ids(),
                                    )
                                except ValueError:
                                    pass
                            (
                                ui.input(
                                    "Generated Directive ID",
                                    value=displayed_directive_id,
                                )
                                .props("filled square dense readonly hide-bottom-space color=accent")
                                .classes("w-full mod-recipe-field")
                            )
                    with ui.element("div").classes("grid grid-cols-1 md:grid-cols-3 gap-3 w-full"):
                        if editor_state.operation is _MinecraftRecipeEditorOperation.ADD:
                            (
                                ui.input(
                                    "Output count",
                                    value=editor_state.output_count_text,
                                    on_change=lambda event: set_output_count(_event_args_as_text(event)),
                                )
                                .props("filled square dense hide-bottom-space color=accent")
                                .classes("w-full mod-recipe-field")
                            )
                        else:
                            (
                                ui.input(
                                    "Remove Mod ID",
                                    value=editor_state.removal_mod_id,
                                    on_change=lambda event: set_removal_mod_id(_event_args_as_text(event)),
                                )
                                .props("filled square dense hide-bottom-space color=accent")
                                .classes("w-full mod-recipe-field")
                            )
                        if editor_state.operation is _MinecraftRecipeEditorOperation.ADD and editor_state.kind in {
                            MinecraftRecipeKind.SMELTING,
                            MinecraftRecipeKind.BLASTING,
                            MinecraftRecipeKind.SMOKING,
                            MinecraftRecipeKind.CAMPFIRE_COOKING,
                        }:
                            (
                                ui.input(
                                    "Experience",
                                    value=editor_state.cooking_experience_text,
                                    on_change=lambda event: set_cooking_experience(_event_args_as_text(event)),
                                )
                                .props("filled square dense clearable hide-bottom-space color=accent")
                                .classes("w-full mod-recipe-field")
                            )
                            (
                                ui.input(
                                    "Cooking Ticks",
                                    value=editor_state.cooking_time_ticks_text,
                                    on_change=lambda event: set_cooking_time_ticks(_event_args_as_text(event)),
                                )
                                .props("filled square dense clearable hide-bottom-space color=accent")
                                .classes("w-full mod-recipe-field")
                            )
                        elif editor_state.operation is _MinecraftRecipeEditorOperation.REMOVE:
                            (
                                ui.input(
                                    "Remove Output",
                                    value=editor_state.removal_output_filter.editor_text,
                                    on_change=lambda event: set_removal_output_filter(_event_args_as_text(event)),
                                )
                                .props("filled square dense clearable hide-bottom-space color=accent")
                                .classes("w-full mod-recipe-field")
                            )
                            (
                                ui.input(
                                    "Remove Input",
                                    value=editor_state.removal_input_filter.editor_text,
                                    on_change=lambda event: set_removal_input_filter(_event_args_as_text(event)),
                                )
                                .props("filled square dense clearable hide-bottom-space color=accent")
                                .classes("w-full mod-recipe-field")
                            )
                    with ui.element("div").classes("mod-recipe-workbench"):
                        with ui.column().classes("mod-recipe-workbench-main"):
                            if editor_state.operation is _MinecraftRecipeEditorOperation.REMOVE:
                                self._render_minecraft_recipe_single_input(
                                    ui=ui,
                                    item=editor_state.removal_output_filter,
                                    item_icon_api_url=item_icon_api_url,
                                    selection=_MinecraftRecipeEditorSelection.removal_output(),
                                    selected_slot=editor_state.selected_slot,
                                    on_select=select_slot,
                                    on_drop=apply_drag_payload_to_slot,
                                    title="Output Filter",
                                )
                                self._render_minecraft_recipe_single_input(
                                    ui=ui,
                                    item=editor_state.removal_input_filter,
                                    item_icon_api_url=item_icon_api_url,
                                    selection=_MinecraftRecipeEditorSelection.removal_input(),
                                    selected_slot=editor_state.selected_slot,
                                    on_select=select_slot,
                                    on_drop=apply_drag_payload_to_slot,
                                    title="Input Filter",
                                )
                            elif editor_state.kind is MinecraftRecipeKind.SHAPELESS:
                                self._render_minecraft_recipe_grid(
                                    ui=ui,
                                    items=tuple(editor_state.shapeless_ingredients),
                                    item_icon_api_url=item_icon_api_url,
                                    selection_factory=_MinecraftRecipeEditorSelection.shapeless,
                                    selected_slot=editor_state.selected_slot,
                                    on_select=select_slot,
                                    on_drop=apply_drag_payload_to_slot,
                                    title="Ingredients",
                                )
                            elif editor_state.kind is MinecraftRecipeKind.SHAPED:
                                self._render_minecraft_recipe_grid(
                                    ui=ui,
                                    items=tuple(editor_state.shaped_ingredients),
                                    item_icon_api_url=item_icon_api_url,
                                    selection_factory=_MinecraftRecipeEditorSelection.shaped,
                                    selected_slot=editor_state.selected_slot,
                                    on_select=select_slot,
                                    on_drop=apply_drag_payload_to_slot,
                                    title="Pattern",
                                )
                            elif editor_state.kind in {
                                MinecraftRecipeKind.SMELTING,
                                MinecraftRecipeKind.BLASTING,
                                MinecraftRecipeKind.SMOKING,
                                MinecraftRecipeKind.CAMPFIRE_COOKING,
                            }:
                                self._render_minecraft_recipe_single_input(
                                    ui=ui,
                                    item=editor_state.cooking_input_ingredient,
                                    item_icon_api_url=item_icon_api_url,
                                    selection=_MinecraftRecipeEditorSelection.cooking_input(),
                                    selected_slot=editor_state.selected_slot,
                                    on_select=select_slot,
                                    on_drop=apply_drag_payload_to_slot,
                                    title="Input",
                                )
                            elif editor_state.kind is MinecraftRecipeKind.STONECUTTING:
                                self._render_minecraft_recipe_single_input(
                                    ui=ui,
                                    item=editor_state.stonecutting_input_ingredient,
                                    item_icon_api_url=item_icon_api_url,
                                    selection=_MinecraftRecipeEditorSelection.stonecutting_input(),
                                    selected_slot=editor_state.selected_slot,
                                    on_select=select_slot,
                                    on_drop=apply_drag_payload_to_slot,
                                    title="Input",
                                )
                            else:
                                raise ValueError(f"Unsupported Minecraft recipe kind: {editor_state.kind.value}")
                        with ui.column().classes("mod-recipe-workbench-side"):
                            if editor_state.operation is _MinecraftRecipeEditorOperation.ADD:
                                with ui.element("div").classes("mod-recipe-input-panel mod-recipe-output-panel"):
                                    ui.label("Output").classes("text-xs uppercase mod-subtitle")
                                    self._render_minecraft_recipe_slot(
                                        ui=ui,
                                        item=editor_state.output_item_id,
                                        item_icon_api_url=item_icon_api_url,
                                        selection=_MinecraftRecipeEditorSelection.output(),
                                        selected_slot=editor_state.selected_slot,
                                        on_select=select_slot,
                                        on_drop=apply_drag_payload_to_slot,
                                    )
                            with ui.element("div").classes("mod-recipe-selection-panel"):
                                ui.label("Selected Slot").classes("text-xs uppercase mod-subtitle")
                                ui.label(self._minecraft_recipe_selection_label(editor_state.selected_slot)).classes(
                                    "text-sm mod-title-small"
                                )
                                selected_ingredient_state = self._minecraft_selected_ingredient_state(editor_state)
                                if selected_ingredient_state is not None:
                                    ingredient_kind_options: dict[str, str] = {
                                        _MinecraftRecipeEditorIngredientKind.ITEM.value: "Item",
                                        _MinecraftRecipeEditorIngredientKind.TAG.value: "Tag",
                                    }
                                    (
                                        ui.select(
                                            ingredient_kind_options,
                                            value=selected_ingredient_state.kind.value,
                                            label="Selected Kind",
                                            on_change=lambda event: set_selected_ingredient_kind(
                                                _event_args_as_text(event)
                                            ),
                                        )
                                        .props(
                                            "filled square dense hide-bottom-space color=accent "
                                            "options-dark popup-content-class=mod-setting-menu"
                                        )
                                        .classes("w-full mod-recipe-field")
                                    )
                                    (
                                        ui.input(
                                            "Selected Resource",
                                            value=selected_ingredient_state.resource_id,
                                            on_change=lambda event: set_selected_ingredient_resource(
                                                _event_args_as_text(event)
                                            ),
                                        )
                                        .props("filled square dense clearable hide-bottom-space color=accent")
                                        .classes("w-full mod-recipe-field")
                                    )
                                with ui.row().classes("mod-recipe-selection-actions"):
                                    ui.button("Clear Selected", on_click=clear_selected_slot).classes(
                                        "mod-list-button secondary"
                                    )
                                    ui.button("Clear Recipe", on_click=clear_recipe).classes(
                                        "mod-list-button secondary"
                                    )
                                    save_button = ui.button(
                                        "Update Recipe"
                                        if editor_state.editing_recipe_index is not None
                                        else "Add Recipe",
                                        on_click=add_recipe,
                                    ).classes("mod-list-button")
                                    if minecraft_username is None:
                                        save_button.disable()

        render_editor()

        with ui.card().classes("mod-card mod-card-plain mod-recipe-browser-shell w-full"):
            with ui.column().classes("w-full gap-3"):
                with ui.element("div").classes("mod-recipe-panel-heading"):
                    ui.label("Item Browser").classes("text-sm font-black mod-title-small")
                    ui.label("Select a slot, then click an item to fill it.").classes("mod-subtitle")
                with ui.row().classes("mod-recipe-browser-toolbar"):
                    search_input = (
                        ui.input("Search items", value=editor_state.search_text)
                        .props(
                            "filled square dense clearable hide-bottom-space color=accent "
                            f"debounce={_SEARCH_INPUT_DEBOUNCE_MILLISECONDS}"
                        )
                        .classes("flex-1 min-w-[16rem] mod-config-search mod-recipe-field")
                    )

                    def _refresh_browser(event: ModWebEventArgumentsContainer) -> None:
                        change_search_text(_event_args_as_text(event))

                    search_input.on("update:model-value", _refresh_browser)
                    (
                        ui.select(
                            namespace_options,
                            value=editor_state.browser_namespace,
                            label="Mod / Namespace",
                            on_change=lambda event: change_browser_namespace(_event_args_as_text(event)),
                        )
                        .props(
                            "filled square dense hide-bottom-space color=accent "
                            "options-dark popup-content-class=mod-setting-menu"
                        )
                        .classes("mod-recipe-browser-filter mod-config-select mod-recipe-field")
                    )
                    item_type_select = (
                        ui.select(
                            item_type_options,
                            value=editor_state.browser_item_type.value,
                            label="Item Type",
                            on_change=lambda event: change_browser_item_type(_event_args_as_text(event)),
                        )
                        .props(
                            "filled square dense hide-bottom-space color=accent "
                            "options-dark popup-content-class=mod-setting-menu"
                        )
                        .classes("mod-recipe-browser-filter mod-config-select mod-recipe-field")
                    )
                    if not item_types_classified:
                        item_type_select.disable()
                        with item_type_select:
                            ui.tooltip("Restart Minecraft to generate item type data.")

                    @ui.refreshable
                    def render_browser_summary() -> None:
                        filtered_entries = self._filtered_minecraft_browser_entries(
                            browser_entries,
                            editor_state.search_text,
                            namespace=editor_state.browser_namespace,
                            item_type=editor_state.browser_item_type,
                        )
                        page_count = self._minecraft_browser_page_count(filtered_entries, page_size=page_size)
                        editor_state.page_index = max(0, min(editor_state.page_index, page_count - 1))
                        with ui.element("div").classes("mod-recipe-browser-status"):
                            ui.label(f"{len(filtered_entries):,} results").classes("mod-subtitle")
                            ui.label(f"Page {editor_state.page_index + 1} / {page_count}").classes("mod-subtitle")
                        with ui.row().classes("gap-2"):
                            prev_button = ui.button("Prev", on_click=lambda: change_page(-1)).classes(
                                "mod-list-button secondary"
                            )
                            next_button = ui.button("Next", on_click=lambda: change_page(1)).classes(
                                "mod-list-button secondary"
                            )
                            if editor_state.page_index <= 0:
                                prev_button.disable()
                            if editor_state.page_index >= page_count - 1:
                                next_button.disable()

                    render_browser_summary()

                @ui.refreshable
                def render_browser_grid() -> None:
                    filtered_entries = self._filtered_minecraft_browser_entries(
                        browser_entries,
                        editor_state.search_text,
                        namespace=editor_state.browser_namespace,
                        item_type=editor_state.browser_item_type,
                    )
                    page_count = self._minecraft_browser_page_count(filtered_entries, page_size=page_size)
                    editor_state.page_index = max(0, min(editor_state.page_index, page_count - 1))
                    start_index = editor_state.page_index * page_size
                    page_entries = filtered_entries[start_index : start_index + page_size]
                    with ui.element("div").classes("mod-recipe-browser-grid"):
                        for entry in page_entries:
                            with ui.card().classes(
                                "mod-card mod-card-plain mod-recipe-browser-card w-full"
                            ) as item_card:
                                item_card.on(
                                    "click",
                                    lambda _event=None, item_id=entry.item_id: apply_item_to_selected_slot(item_id),
                                )
                                item_card.props("draggable=true")
                                item_card.on(
                                    "dragstart",
                                    js_handler=self._minecraft_recipe_drag_start_js_handler(
                                        _MinecraftRecipeDragPayload(
                                            kind=_MinecraftRecipeEditorIngredientKind.ITEM,
                                            resource_id=entry.item_id,
                                        )
                                    ),
                                )
                                with ui.row().classes("mod-recipe-browser-card-row"):
                                    with ui.column().classes("mod-recipe-browser-copy"):
                                        ui.label(entry.display_name).classes("mod-recipe-browser-name")
                                        ui.label(entry.item_id).classes("mod-recipe-browser-id")
                                    ui.html(
                                        self._minecraft_item_icon_markup(
                                            item_icon_api_url=item_icon_api_url,
                                            item_id=entry.item_id,
                                            alt_text=entry.display_name,
                                        )
                                    ).classes("mod-recipe-browser-visual")

                render_browser_grid()
        render_browser_summary.refresh()
        render_browser_grid.refresh()
        return refresh_all

    def _render_minecraft_recipe_manage_panel(
        self,
        *,
        ui: ModWebUi,
        model: ModWebPageModel,
        user: ModWebUser,
        editor_state: _MinecraftRecipeEditorState,
        on_edit: Callable[[int, MinecraftRecipeMutation], None],
    ) -> None:
        del editor_state
        summary = model.minecraft_recipes
        if summary is None or summary.load_error is not None or not summary.entries:
            ui.html(self._minecraft_recipes_body_markup(summary)).classes("w-full")
            return
        mutations = self._minecraft_recipe_mutations(summary)
        can_write = self._user_has_level(user, Power_Level.sudo)
        with ui.column().classes("mod-recipe-manage-list w-full gap-3"):
            for mutation_index, (entry, mutation) in enumerate(zip(summary.entries, mutations, strict=False)):
                editable = self._minecraft_recipe_can_edit_in_basic_editor(mutation)

                async def delete_recipe(index: int = mutation_index) -> None:
                    try:
                        await self._delete_minecraft_recipe_mutation(model=model, mutation_index=index, user=user)
                    except Exception as xcp:
                        ui.notify(f"Recipe delete failed: {xcp}", type="negative")
                        return
                    ui.notify("Recipe deleted.", type="positive")
                    ui.navigate.reload()

                with ui.card().classes("mod-card mod-card-plain mod-recipe-manage-card w-full"):
                    with ui.column().classes("w-full gap-2"):
                        with ui.row().classes("mod-recipe-manage-header"):
                            with ui.column().classes("gap-1"):
                                operation_label = (
                                    "Add" if entry.operation is ModWebMinecraftRecipeOperationKind.ADD else "Remove"
                                )
                                operation_class = (
                                    "mod-recipe-operation-add"
                                    if entry.operation is ModWebMinecraftRecipeOperationKind.ADD
                                    else "mod-recipe-operation-remove"
                                )
                                ui.html(
                                    (
                                        '<div class="mod-recipe-manage-badges">'
                                        f'<span class="mod-recipe-operation {operation_class}">{escape(operation_label)}</span>'
                                        f'<span class="mod-recipe-kind">{escape(entry.kind_label)}</span>'
                                        "</div>"
                                    )
                                )
                                ui.label(entry.title).classes("text-base font-semibold")
                            with ui.row().classes("gap-2 flex-wrap"):
                                edit_button = ui.button(
                                    "Edit",
                                    on_click=lambda _event=None, index=mutation_index, current_mutation=mutation: (
                                        on_edit(index, current_mutation)
                                    ),
                                ).classes("mod-list-button secondary")
                                delete_button = ui.button("Delete", on_click=delete_recipe).classes(
                                    "mod-list-button danger"
                                )
                                if not can_write:
                                    edit_button.disable()
                                    delete_button.disable()
                                elif not editable:
                                    edit_button.disable()
                        ui.label(entry.detail).classes("mod-subtitle text-sm")
                        if entry.recipe_id is not None:
                            ui.label(f"ID: {entry.recipe_id}").classes("mod-subtitle text-xs break-all")
                        if not editable:
                            ui.label("This entry cannot be loaded into the basic editor yet.").classes(
                                "mod-subtitle text-xs"
                            )

    @staticmethod
    def _minecraft_known_item_ids(summary: ModWebMinecraftItemRegistrySummary | None) -> tuple[str, ...]:
        if summary is None or summary.load_error is not None:
            return ()
        return summary.item_ids

    @staticmethod
    def _minecraft_recipe_mutations(
        summary: ModWebMinecraftRecipeBookSummary | None,
    ) -> tuple[MinecraftRecipeMutation, ...]:
        if summary is None or summary.load_error is not None:
            return ()
        if not summary.mutation_mappings:
            return ()
        recipe_book = MinecraftRecipeBook.from_mapping(
            {
                "schema_version": MinecraftRecipeBook.empty().schema_version,
                "mutations": list(summary.mutation_mappings),
            }
        )
        return recipe_book.mutations

    @staticmethod
    def _reset_minecraft_recipe_editor(editor_state: _MinecraftRecipeEditorState) -> None:
        editor_state.operation = _MinecraftRecipeEditorOperation.ADD
        editor_state.kind = MinecraftRecipeKind.SHAPELESS
        editor_state.editing_recipe_index = None
        editor_state.recipe_id = ""
        editor_state.output_item_id = ""
        editor_state.output_count_text = "1"
        editor_state.shapeless_ingredients = [_MinecraftRecipeEditorIngredientState.empty() for _ in range(9)]
        editor_state.shaped_ingredients = [_MinecraftRecipeEditorIngredientState.empty() for _ in range(9)]
        editor_state.cooking_input_ingredient = _MinecraftRecipeEditorIngredientState.empty()
        editor_state.cooking_experience_text = ""
        editor_state.cooking_time_ticks_text = ""
        editor_state.stonecutting_input_ingredient = _MinecraftRecipeEditorIngredientState.empty()
        editor_state.removal_recipe_id = ""
        editor_state.removal_output_filter = _MinecraftRecipeEditorIngredientState.empty()
        editor_state.removal_input_filter = _MinecraftRecipeEditorIngredientState.empty()
        editor_state.removal_recipe_type_text = ""
        editor_state.removal_mod_id = ""
        editor_state.selected_slot = _MinecraftRecipeEditorSelection.output()

    @staticmethod
    def _minecraft_recipe_can_edit_in_basic_editor(mutation: MinecraftRecipeMutation) -> bool:
        return isinstance(
            mutation,
            (
                MinecraftShapelessRecipe,
                MinecraftShapedRecipe,
                MinecraftCookingRecipe,
                MinecraftStonecuttingRecipe,
                MinecraftRecipeRemoval,
            ),
        )

    @classmethod
    def _load_minecraft_recipe_editor_state(
        cls,
        editor_state: _MinecraftRecipeEditorState,
        mutation: MinecraftRecipeMutation,
        *,
        mutation_index: int,
    ) -> None:
        if not cls._minecraft_recipe_can_edit_in_basic_editor(mutation):
            raise ValueError("This recipe cannot be edited in the basic recipe editor yet.")
        cls._reset_minecraft_recipe_editor(editor_state)
        editor_state.editing_recipe_index = mutation_index
        if isinstance(mutation, MinecraftShapelessRecipe):
            editor_state.operation = _MinecraftRecipeEditorOperation.ADD
            editor_state.kind = MinecraftRecipeKind.SHAPELESS
            editor_state.recipe_id = mutation.recipe_id or ""
            editor_state.output_item_id = mutation.output.item_id
            editor_state.output_count_text = str(mutation.output.count)
            slot_index = 0
            for ingredient in mutation.ingredients:
                for _ in range(ingredient.count):
                    editor_state.shapeless_ingredients[slot_index] = (
                        cls._minecraft_editor_ingredient_from_recipe_ingredient(ingredient)
                    )
                    slot_index += 1
            editor_state.selected_slot = _MinecraftRecipeEditorSelection.shapeless(0)
            return
        if isinstance(mutation, MinecraftShapedRecipe):
            editor_state.operation = _MinecraftRecipeEditorOperation.ADD
            editor_state.kind = MinecraftRecipeKind.SHAPED
            editor_state.recipe_id = mutation.recipe_id or ""
            editor_state.output_item_id = mutation.output.item_id
            editor_state.output_count_text = str(mutation.output.count)
            for row_index, row in enumerate(mutation.pattern):
                for column_index, symbol in enumerate(row):
                    if symbol == " ":
                        continue
                    ingredient = mutation.key[symbol]
                    editor_state.shaped_ingredients[row_index * 3 + column_index] = (
                        cls._minecraft_editor_ingredient_from_recipe_ingredient(ingredient)
                    )
            editor_state.selected_slot = _MinecraftRecipeEditorSelection.shaped(0)
            return
        if isinstance(mutation, MinecraftCookingRecipe):
            editor_state.operation = _MinecraftRecipeEditorOperation.ADD
            editor_state.kind = mutation.kind
            editor_state.recipe_id = mutation.recipe_id or ""
            editor_state.output_item_id = mutation.output.item_id
            editor_state.output_count_text = str(mutation.output.count)
            editor_state.cooking_input_ingredient = cls._minecraft_editor_ingredient_from_recipe_ingredient(
                mutation.ingredient
            )
            editor_state.cooking_experience_text = "" if mutation.experience is None else f"{mutation.experience:g}"
            editor_state.cooking_time_ticks_text = (
                "" if mutation.cooking_time_ticks is None else str(mutation.cooking_time_ticks)
            )
            editor_state.selected_slot = _MinecraftRecipeEditorSelection.cooking_input()
            return
        if isinstance(mutation, MinecraftStonecuttingRecipe):
            editor_state.operation = _MinecraftRecipeEditorOperation.ADD
            editor_state.kind = MinecraftRecipeKind.STONECUTTING
            editor_state.recipe_id = mutation.recipe_id or ""
            editor_state.output_item_id = mutation.output.item_id
            editor_state.output_count_text = str(mutation.output.count)
            editor_state.stonecutting_input_ingredient = cls._minecraft_editor_ingredient_from_recipe_ingredient(
                mutation.ingredient
            )
            editor_state.selected_slot = _MinecraftRecipeEditorSelection.stonecutting_input()
            return
        if isinstance(mutation, MinecraftRecipeRemoval):
            editor_state.operation = _MinecraftRecipeEditorOperation.REMOVE
            editor_state.recipe_id = mutation.directive_id or ""
            editor_state.removal_recipe_id = mutation.filter.recipe_id or ""
            editor_state.removal_output_filter = cls._minecraft_optional_editor_ingredient_from_recipe_ingredient(
                mutation.filter.output
            )
            editor_state.removal_input_filter = cls._minecraft_optional_editor_ingredient_from_recipe_ingredient(
                mutation.filter.input
            )
            editor_state.removal_recipe_type_text = cls._minecraft_optional_recipe_type_editor_text(
                mutation.filter.recipe_type
            )
            editor_state.removal_mod_id = mutation.filter.mod_id or ""
            if editor_state.removal_output_filter.has_value:
                editor_state.selected_slot = _MinecraftRecipeEditorSelection.removal_output()
            elif editor_state.removal_input_filter.has_value:
                editor_state.selected_slot = _MinecraftRecipeEditorSelection.removal_input()
            else:
                editor_state.selected_slot = _MinecraftRecipeEditorSelection.removal_output()
            return
        raise ValueError("Unsupported recipe mutation for the basic recipe editor.")

    @staticmethod
    def _minecraft_editor_ingredient_from_recipe_ingredient(
        ingredient: MinecraftRecipeIngredient,
    ) -> _MinecraftRecipeEditorIngredientState:
        if ingredient.kind.value == "tag":
            return _MinecraftRecipeEditorIngredientState.tag(ingredient.resource_id)
        return _MinecraftRecipeEditorIngredientState.item(ingredient.resource_id)

    @classmethod
    def _minecraft_optional_editor_ingredient_from_recipe_ingredient(
        cls,
        ingredient: MinecraftRecipeIngredient | None,
    ) -> _MinecraftRecipeEditorIngredientState:
        if ingredient is None:
            return _MinecraftRecipeEditorIngredientState.empty()
        return cls._minecraft_editor_ingredient_from_recipe_ingredient(ingredient)

    @staticmethod
    def _minecraft_optional_recipe_type_editor_text(recipe_type: MinecraftRecipeKind | str | None) -> str:
        if recipe_type is None:
            return ""
        return recipe_type.value if isinstance(recipe_type, MinecraftRecipeKind) else recipe_type

    @staticmethod
    def _minecraft_item_display_name(item_id: str) -> str:
        namespace, _, path = item_id.partition(":")
        del namespace
        display_text = path or item_id
        return " ".join(part.capitalize() for part in display_text.replace("/", " ").replace("_", " ").split())

    @staticmethod
    def _minecraft_namespace_display_name(namespace: str) -> str:
        return " ".join(part.capitalize() for part in namespace.replace("-", " ").replace("_", " ").split())

    @classmethod
    def _minecraft_browser_entries(
        cls,
        item_ids: tuple[str, ...],
        *,
        block_item_ids: tuple[str, ...] = (),
    ) -> tuple[_MinecraftRecipeBrowserEntry, ...]:
        block_item_id_set = frozenset(block_item_ids)
        return tuple(
            _MinecraftRecipeBrowserEntry(
                item_id=item_id,
                display_name=cls._minecraft_item_display_name(item_id),
                namespace=item_id.partition(":")[0],
                item_type=(
                    _MinecraftRecipeBrowserItemType.BLOCK
                    if item_id in block_item_id_set
                    else _MinecraftRecipeBrowserItemType.ITEM
                ),
            )
            for item_id in item_ids
        )

    @staticmethod
    def _filtered_minecraft_browser_entries(
        entries: tuple[_MinecraftRecipeBrowserEntry, ...],
        search_text: str,
        *,
        namespace: str = "",
        item_type: _MinecraftRecipeBrowserItemType = _MinecraftRecipeBrowserItemType.ALL,
    ) -> tuple[_MinecraftRecipeBrowserEntry, ...]:
        needle = search_text.strip().casefold()
        return tuple(
            entry
            for entry in entries
            if (not namespace or entry.namespace == namespace)
            and (item_type is _MinecraftRecipeBrowserItemType.ALL or entry.item_type is item_type)
            and (not needle or needle in entry.item_id.casefold() or needle in entry.display_name.casefold())
        )

    @staticmethod
    def _minecraft_browser_page_count(
        entries: tuple[_MinecraftRecipeBrowserEntry, ...],
        *,
        page_size: int,
    ) -> int:
        if page_size <= 0:
            raise ValueError("Minecraft browser page size must be positive.")
        return max(1, (len(entries) + page_size - 1) // page_size)

    @staticmethod
    def _minecraft_recipe_selection_label(selection: _MinecraftRecipeEditorSelection) -> str:
        if selection.area is _MinecraftRecipeEditorArea.OUTPUT:
            return "Output"
        if selection.area is _MinecraftRecipeEditorArea.SHAPELESS:
            assert selection.index is not None
            return f"Shapeless Slot {selection.index + 1}"
        if selection.area is _MinecraftRecipeEditorArea.SHAPED:
            assert selection.index is not None
            row_index, column_index = divmod(selection.index, 3)
            return f"Shaped Row {row_index + 1}, Column {column_index + 1}"
        if selection.area is _MinecraftRecipeEditorArea.COOKING_INPUT:
            return "Input"
        if selection.area is _MinecraftRecipeEditorArea.STONECUTTING_INPUT:
            return "Input"
        if selection.area is _MinecraftRecipeEditorArea.REMOVAL_OUTPUT:
            return "Removal Output Filter"
        if selection.area is _MinecraftRecipeEditorArea.REMOVAL_INPUT:
            return "Removal Input Filter"
        raise ValueError(f"Unsupported Minecraft recipe editor area: {selection.area.value}")

    @staticmethod
    def _minecraft_selected_ingredient_state(
        editor_state: _MinecraftRecipeEditorState,
        selection: _MinecraftRecipeEditorSelection | None = None,
    ) -> _MinecraftRecipeEditorIngredientState | None:
        resolved_selection = editor_state.selected_slot if selection is None else selection
        if resolved_selection.area is _MinecraftRecipeEditorArea.SHAPELESS:
            assert resolved_selection.index is not None
            return editor_state.shapeless_ingredients[resolved_selection.index]
        if resolved_selection.area is _MinecraftRecipeEditorArea.SHAPED:
            assert resolved_selection.index is not None
            return editor_state.shaped_ingredients[resolved_selection.index]
        if resolved_selection.area is _MinecraftRecipeEditorArea.COOKING_INPUT:
            return editor_state.cooking_input_ingredient
        if resolved_selection.area is _MinecraftRecipeEditorArea.STONECUTTING_INPUT:
            return editor_state.stonecutting_input_ingredient
        if resolved_selection.area is _MinecraftRecipeEditorArea.REMOVAL_OUTPUT:
            return editor_state.removal_output_filter
        if resolved_selection.area is _MinecraftRecipeEditorArea.REMOVAL_INPUT:
            return editor_state.removal_input_filter
        return None

    @staticmethod
    def _sync_minecraft_recipe_selection(editor_state: _MinecraftRecipeEditorState) -> None:
        if editor_state.operation is _MinecraftRecipeEditorOperation.REMOVE:
            if editor_state.selected_slot.area in {
                _MinecraftRecipeEditorArea.REMOVAL_OUTPUT,
                _MinecraftRecipeEditorArea.REMOVAL_INPUT,
            }:
                return
            editor_state.selected_slot = _MinecraftRecipeEditorSelection.removal_output()
            return
        if editor_state.kind is MinecraftRecipeKind.SHAPELESS:
            if editor_state.selected_slot.area in {
                _MinecraftRecipeEditorArea.OUTPUT,
                _MinecraftRecipeEditorArea.SHAPELESS,
            }:
                return
            editor_state.selected_slot = _MinecraftRecipeEditorSelection.shapeless(0)
            return
        if editor_state.kind is MinecraftRecipeKind.SHAPED:
            if editor_state.selected_slot.area in {
                _MinecraftRecipeEditorArea.OUTPUT,
                _MinecraftRecipeEditorArea.SHAPED,
            }:
                return
            editor_state.selected_slot = _MinecraftRecipeEditorSelection.shaped(0)
            return
        if editor_state.kind in {
            MinecraftRecipeKind.SMELTING,
            MinecraftRecipeKind.BLASTING,
            MinecraftRecipeKind.SMOKING,
            MinecraftRecipeKind.CAMPFIRE_COOKING,
        }:
            if editor_state.selected_slot.area in {
                _MinecraftRecipeEditorArea.OUTPUT,
                _MinecraftRecipeEditorArea.COOKING_INPUT,
            }:
                return
            editor_state.selected_slot = _MinecraftRecipeEditorSelection.cooking_input()
            return
        if editor_state.kind is MinecraftRecipeKind.STONECUTTING:
            if editor_state.selected_slot.area in {
                _MinecraftRecipeEditorArea.OUTPUT,
                _MinecraftRecipeEditorArea.STONECUTTING_INPUT,
            }:
                return
            editor_state.selected_slot = _MinecraftRecipeEditorSelection.stonecutting_input()
            return
        raise ValueError(f"Unsupported Minecraft recipe kind: {editor_state.kind.value}")

    @staticmethod
    def _apply_minecraft_recipe_item_to_selection(editor_state: _MinecraftRecipeEditorState, item_id: str) -> None:
        selection = editor_state.selected_slot
        if selection.area is _MinecraftRecipeEditorArea.OUTPUT:
            editor_state.output_item_id = item_id
            return
        if selection.area is _MinecraftRecipeEditorArea.SHAPELESS:
            assert selection.index is not None
            editor_state.shapeless_ingredients[selection.index] = _MinecraftRecipeEditorIngredientState.item(item_id)
            return
        if selection.area is _MinecraftRecipeEditorArea.SHAPED:
            assert selection.index is not None
            editor_state.shaped_ingredients[selection.index] = _MinecraftRecipeEditorIngredientState.item(item_id)
            return
        if selection.area is _MinecraftRecipeEditorArea.COOKING_INPUT:
            editor_state.cooking_input_ingredient = _MinecraftRecipeEditorIngredientState.item(item_id)
            return
        if selection.area is _MinecraftRecipeEditorArea.STONECUTTING_INPUT:
            editor_state.stonecutting_input_ingredient = _MinecraftRecipeEditorIngredientState.item(item_id)
            return
        if selection.area is _MinecraftRecipeEditorArea.REMOVAL_OUTPUT:
            editor_state.removal_output_filter = _MinecraftRecipeEditorIngredientState.item(item_id)
            return
        if selection.area is _MinecraftRecipeEditorArea.REMOVAL_INPUT:
            editor_state.removal_input_filter = _MinecraftRecipeEditorIngredientState.item(item_id)
            return
        raise ValueError(f"Unsupported Minecraft recipe editor area: {selection.area.value}")

    @classmethod
    def _apply_minecraft_recipe_drag_payload_to_selection(
        cls,
        editor_state: _MinecraftRecipeEditorState,
        *,
        selection: _MinecraftRecipeEditorSelection,
        payload: _MinecraftRecipeDragPayload,
    ) -> None:
        if selection.area is _MinecraftRecipeEditorArea.OUTPUT:
            if payload.kind is not _MinecraftRecipeEditorIngredientKind.ITEM:
                raise ValueError("Recipe outputs must be concrete items.")
            editor_state.output_item_id = payload.resource_id
            return
        target_ingredient_state = cls._minecraft_selected_ingredient_state(editor_state, selection)
        if target_ingredient_state is None:
            raise ValueError(f"Unsupported Minecraft recipe editor area: {selection.area.value}")
        target_ingredient_state.kind = payload.kind
        target_ingredient_state.resource_id = payload.resource_id

    @classmethod
    def _clear_minecraft_recipe_selection(cls, editor_state: _MinecraftRecipeEditorState) -> None:
        selection = editor_state.selected_slot
        if selection.area is _MinecraftRecipeEditorArea.OUTPUT:
            editor_state.output_item_id = ""
            return
        ingredient_state = cls._minecraft_selected_ingredient_state(editor_state, selection)
        if ingredient_state is not None:
            ingredient_state.kind = _MinecraftRecipeEditorIngredientKind.ITEM
            ingredient_state.resource_id = ""
            return
        raise ValueError(f"Unsupported Minecraft recipe editor area: {selection.area.value}")

    @classmethod
    def _minecraft_recipe_grid_slot_label(
        cls,
        item: str | _MinecraftRecipeEditorIngredientState,
    ) -> str:
        if isinstance(item, _MinecraftRecipeEditorIngredientState):
            if not item.has_value:
                return "Empty"
            if item.kind is _MinecraftRecipeEditorIngredientKind.TAG:
                return f"Tag: {item.resource_id}"
            item_id = item.resource_id
        else:
            item_id = item
        if not item_id:
            return "Empty"
        return cls._minecraft_item_display_name(item_id)

    @staticmethod
    def _minecraft_item_icon_url(item_icon_api_url: str | None, item_id: str) -> str | None:
        if item_icon_api_url is None:
            return None
        normalised_item_id = item_id.strip()
        if not normalised_item_id:
            return None
        return f"{item_icon_api_url}?{urlencode({'item_id': normalised_item_id})}"

    @staticmethod
    def _minecraft_recipe_drag_data_mime_type() -> str:
        return "application/x-yukibot-minecraft-recipe"

    @classmethod
    def _minecraft_recipe_drag_payload_for_item(
        cls,
        item: str | _MinecraftRecipeEditorIngredientState,
    ) -> _MinecraftRecipeDragPayload | None:
        if isinstance(item, _MinecraftRecipeEditorIngredientState):
            if not item.has_value:
                return None
            return _MinecraftRecipeDragPayload(kind=item.kind, resource_id=item.resource_id)
        item_id = item.strip()
        if not item_id:
            return None
        return _MinecraftRecipeDragPayload(kind=_MinecraftRecipeEditorIngredientKind.ITEM, resource_id=item_id)

    @classmethod
    def _minecraft_recipe_drag_start_js_handler(
        cls,
        payload: _MinecraftRecipeDragPayload,
    ) -> str:
        encoded_payload = json.dumps(payload.to_mapping(), sort_keys=True)
        encoded_mime_type = json.dumps(cls._minecraft_recipe_drag_data_mime_type())
        return (
            "(event) => {"
            "event.stopPropagation();"
            "if (!event.dataTransfer) { return; }"
            f"event.dataTransfer.setData({encoded_mime_type}, JSON.stringify({encoded_payload}));"
            "event.dataTransfer.effectAllowed = 'copy';"
            "}"
        )

    @classmethod
    def _minecraft_recipe_drag_over_js_handler(cls) -> str:
        return (
            "(event) => {"
            "event.preventDefault();"
            "event.stopPropagation();"
            "if (event.dataTransfer) { event.dataTransfer.dropEffect = 'copy'; }"
            "event.currentTarget.classList.add('mod-recipe-slot-drop-active');"
            "}"
        )

    @classmethod
    def _minecraft_recipe_drag_leave_js_handler(cls) -> str:
        return "(event) => { event.currentTarget.classList.remove('mod-recipe-slot-drop-active'); }"

    @classmethod
    def _minecraft_recipe_drop_js_handler(cls) -> str:
        encoded_mime_type = json.dumps(cls._minecraft_recipe_drag_data_mime_type())
        return (
            "(event) => {"
            "event.preventDefault();"
            "event.stopPropagation();"
            "event.currentTarget.classList.remove('mod-recipe-slot-drop-active');"
            "if (!event.dataTransfer) { return; }"
            f"const raw = event.dataTransfer.getData({encoded_mime_type});"
            "if (!raw) { return; }"
            "try { emit(JSON.parse(raw)); } catch (_error) {}"
            "}"
        )

    @classmethod
    def _minecraft_item_icon_markup(
        cls,
        *,
        item_icon_api_url: str | None,
        item_id: str,
        alt_text: str,
    ) -> str:
        icon_url = cls._minecraft_item_icon_url(item_icon_api_url, item_id)
        if icon_url is None:
            return '<div class="mod-recipe-icon-shell mod-recipe-icon-fallback">?</div>'
        return (
            f'<div class="mod-recipe-icon-stack" role="img" aria-label="{escape(alt_text)}">'
            '<div class="mod-recipe-icon-shell mod-recipe-icon-fallback" aria-hidden="true"></div>'
            f'<img class="mod-recipe-icon-shell mod-recipe-icon-image" src="{escape(icon_url)}" '
            'alt="" aria-hidden="true" loading="lazy" decoding="async">'
            "</div>"
        )

    @classmethod
    def _minecraft_recipe_slot_icon_markup(
        cls,
        *,
        item_icon_api_url: str | None,
        item: str | _MinecraftRecipeEditorIngredientState,
    ) -> str:
        if isinstance(item, _MinecraftRecipeEditorIngredientState):
            if not item.has_value:
                return '<div class="mod-recipe-icon-shell mod-recipe-icon-empty">+</div>'
            if item.kind is _MinecraftRecipeEditorIngredientKind.TAG:
                return '<div class="mod-recipe-icon-shell mod-recipe-icon-tag">#</div>'
            item_id = item.resource_id
        else:
            item_id = item
        if not item_id:
            return '<div class="mod-recipe-icon-shell mod-recipe-icon-empty">+</div>'
        return cls._minecraft_item_icon_markup(
            item_icon_api_url=item_icon_api_url,
            item_id=item_id,
            alt_text=cls._minecraft_recipe_grid_slot_label(item),
        )

    @classmethod
    def _render_minecraft_recipe_slot(
        cls,
        *,
        ui: ModWebUi,
        item: str | _MinecraftRecipeEditorIngredientState,
        item_icon_api_url: str | None,
        selection: _MinecraftRecipeEditorSelection,
        selected_slot: _MinecraftRecipeEditorSelection,
        on_select: Callable[[_MinecraftRecipeEditorSelection], None],
        on_drop: Callable[[_MinecraftRecipeEditorSelection, object], None],
    ) -> None:
        selected = selection == selected_slot
        item_text = item if isinstance(item, str) else item.editor_text
        drag_payload = cls._minecraft_recipe_drag_payload_for_item(item)
        button_classes = "mod-recipe-slot w-full"
        if selected:
            button_classes = f"{button_classes} mod-recipe-slot-selected"
        button = ui.button(on_click=lambda: on_select(selection)).classes(button_classes)
        button_props = "flat no-caps"
        if drag_payload is not None:
            button_props = f"{button_props} draggable=true"
        button.props(button_props)
        if drag_payload is not None:
            button.on("dragstart", js_handler=cls._minecraft_recipe_drag_start_js_handler(drag_payload))
        button.on("dragover", js_handler=cls._minecraft_recipe_drag_over_js_handler())
        button.on("dragleave", js_handler=cls._minecraft_recipe_drag_leave_js_handler())
        button.on(
            "drop",
            lambda event, target_selection=selection: on_drop(target_selection, _value_as_object(event)),
            js_handler=cls._minecraft_recipe_drop_js_handler(),
        )
        with button:
            with ui.row().classes("mod-recipe-slot-head"):
                with ui.column().classes("mod-recipe-slot-copy"):
                    ui.label(cls._minecraft_recipe_grid_slot_label(item)).classes("mod-recipe-slot-label")
                    value_classes = "mod-recipe-slot-value"
                    if (
                        isinstance(item, _MinecraftRecipeEditorIngredientState)
                        and item.kind is _MinecraftRecipeEditorIngredientKind.TAG
                    ):
                        value_classes = f"{value_classes} mod-recipe-slot-value-tag"
                    elif not item_text:
                        value_classes = f"{value_classes} mod-recipe-slot-value-empty"
                    ui.label(item_text or "Click an item below").classes(value_classes)
                ui.html(cls._minecraft_recipe_slot_icon_markup(item_icon_api_url=item_icon_api_url, item=item)).classes(
                    "mod-recipe-slot-visual"
                )

    @classmethod
    def _render_minecraft_recipe_grid(
        cls,
        *,
        ui: ModWebUi,
        items: tuple[_MinecraftRecipeEditorIngredientState, ...],
        item_icon_api_url: str | None,
        selection_factory: Callable[[int], _MinecraftRecipeEditorSelection],
        selected_slot: _MinecraftRecipeEditorSelection,
        on_select: Callable[[_MinecraftRecipeEditorSelection], None],
        on_drop: Callable[[_MinecraftRecipeEditorSelection, object], None],
        title: str,
    ) -> None:
        with ui.element("div").classes("mod-recipe-input-panel"):
            ui.label(title).classes("text-xs uppercase mod-subtitle")
            with ui.element("div").classes("mod-recipe-slot-grid"):
                for index, item in enumerate(items):
                    cls._render_minecraft_recipe_slot(
                        ui=ui,
                        item=item,
                        item_icon_api_url=item_icon_api_url,
                        selection=selection_factory(index),
                        selected_slot=selected_slot,
                        on_select=on_select,
                        on_drop=on_drop,
                    )

    @classmethod
    def _render_minecraft_recipe_single_input(
        cls,
        *,
        ui: ModWebUi,
        item: _MinecraftRecipeEditorIngredientState,
        item_icon_api_url: str | None,
        selection: _MinecraftRecipeEditorSelection,
        selected_slot: _MinecraftRecipeEditorSelection,
        on_select: Callable[[_MinecraftRecipeEditorSelection], None],
        on_drop: Callable[[_MinecraftRecipeEditorSelection, object], None],
        title: str,
    ) -> None:
        with ui.element("div").classes("mod-recipe-input-panel"):
            ui.label(title).classes("text-xs uppercase mod-subtitle")
            cls._render_minecraft_recipe_slot(
                ui=ui,
                item=item,
                item_icon_api_url=item_icon_api_url,
                selection=selection,
                selected_slot=selected_slot,
                on_select=on_select,
                on_drop=on_drop,
            )

    @classmethod
    def _minecraft_recipe_mutation_from_editor(
        cls, editor_state: _MinecraftRecipeEditorState
    ) -> MinecraftRecipeMutation:
        if editor_state.operation is _MinecraftRecipeEditorOperation.REMOVE:
            return MinecraftRecipeRemoval(
                filter=MinecraftRecipeRemovalFilter(
                    recipe_id=editor_state.removal_recipe_id.strip() or None,
                    output=cls._minecraft_optional_recipe_ingredient_from_editor_state(
                        editor_state.removal_output_filter
                    ),
                    input=cls._minecraft_optional_recipe_ingredient_from_editor_state(
                        editor_state.removal_input_filter
                    ),
                    recipe_type=cls._parse_optional_minecraft_recipe_type(editor_state.removal_recipe_type_text),
                    mod_id=editor_state.removal_mod_id.strip() or None,
                ),
                directive_id=editor_state.recipe_id.strip() or None,
            )
        output_item_id = editor_state.output_item_id.strip()
        if not output_item_id:
            raise ValueError("Choose an output item.")
        output = MinecraftRecipeItemStack(
            output_item_id,
            count=cls._parse_minecraft_recipe_count(editor_state.output_count_text),
        )
        recipe_id = editor_state.recipe_id.strip() or None
        if editor_state.kind is MinecraftRecipeKind.SHAPELESS:
            ingredient_counts: dict[tuple[_MinecraftRecipeEditorIngredientKind, str], int] = {}
            for ingredient_state in editor_state.shapeless_ingredients:
                if not ingredient_state.has_value:
                    continue
                ingredient_key = (ingredient_state.kind, ingredient_state.resource_id.strip())
                ingredient_counts[ingredient_key] = ingredient_counts.get(ingredient_key, 0) + 1
            ingredients = tuple(
                MinecraftRecipeIngredient.item(resource_id, count=count)
                if kind is _MinecraftRecipeEditorIngredientKind.ITEM
                else MinecraftRecipeIngredient.tag(resource_id, count=count)
                for (kind, resource_id), count in ingredient_counts.items()
            )
            if not ingredients:
                raise ValueError("Choose at least one shapeless ingredient.")
            return MinecraftShapelessRecipe(output=output, ingredients=ingredients, recipe_id=recipe_id)
        if editor_state.kind is MinecraftRecipeKind.SHAPED:
            pattern, key = cls._minecraft_shaped_pattern_and_key(tuple(editor_state.shaped_ingredients))
            return MinecraftShapedRecipe(output=output, pattern=pattern, key=key, recipe_id=recipe_id)
        if editor_state.kind in {
            MinecraftRecipeKind.SMELTING,
            MinecraftRecipeKind.BLASTING,
            MinecraftRecipeKind.SMOKING,
            MinecraftRecipeKind.CAMPFIRE_COOKING,
        }:
            ingredient = cls._minecraft_optional_recipe_ingredient_from_editor_state(
                editor_state.cooking_input_ingredient
            )
            if ingredient is None:
                raise ValueError("Choose an input item.")
            return MinecraftCookingRecipe(
                kind=editor_state.kind,
                output=output,
                ingredient=ingredient,
                experience=cls._parse_optional_minecraft_recipe_float(editor_state.cooking_experience_text),
                cooking_time_ticks=cls._parse_optional_minecraft_recipe_int(editor_state.cooking_time_ticks_text),
                recipe_id=recipe_id,
            )
        if editor_state.kind is MinecraftRecipeKind.STONECUTTING:
            ingredient = cls._minecraft_optional_recipe_ingredient_from_editor_state(
                editor_state.stonecutting_input_ingredient
            )
            if ingredient is None:
                raise ValueError("Choose an input item.")
            return MinecraftStonecuttingRecipe(
                output=output,
                ingredient=ingredient,
                recipe_id=recipe_id,
            )
        raise ValueError(f"Unsupported Minecraft recipe kind: {editor_state.kind.value}")

    @classmethod
    def _minecraft_shaped_pattern_and_key(
        cls,
        shaped_ingredients: tuple[_MinecraftRecipeEditorIngredientState, ...],
    ) -> tuple[tuple[str, ...], dict[str, MinecraftRecipeIngredient]]:
        if len(shaped_ingredients) != 9:
            raise ValueError("Shaped recipe grids must contain exactly 9 slots.")
        occupied_indices = [
            index for index, ingredient_state in enumerate(shaped_ingredients) if ingredient_state.has_value
        ]
        if not occupied_indices:
            raise ValueError("Choose at least one shaped ingredient.")
        occupied_rows = [index // 3 for index in occupied_indices]
        occupied_columns = [index % 3 for index in occupied_indices]
        row_start = min(occupied_rows)
        row_end = max(occupied_rows)
        column_start = min(occupied_columns)
        column_end = max(occupied_columns)
        symbol_by_ingredient_text: dict[str, str] = {}
        key: dict[str, MinecraftRecipeIngredient] = {}
        next_symbol_code = ord("A")
        pattern_rows: list[str] = []
        for row_index in range(row_start, row_end + 1):
            pattern_characters: list[str] = []
            for column_index in range(column_start, column_end + 1):
                ingredient_state = shaped_ingredients[row_index * 3 + column_index]
                if not ingredient_state.has_value:
                    pattern_characters.append(" ")
                    continue
                ingredient_text = ingredient_state.editor_text
                if ingredient_text not in symbol_by_ingredient_text:
                    if next_symbol_code > ord("Z"):
                        raise ValueError("Shaped recipes currently support up to 26 unique ingredients.")
                    symbol = chr(next_symbol_code)
                    next_symbol_code += 1
                    symbol_by_ingredient_text[ingredient_text] = symbol
                    ingredient = cls._minecraft_optional_recipe_ingredient_from_editor_state(ingredient_state)
                    assert ingredient is not None
                    key[symbol] = ingredient
                pattern_characters.append(symbol_by_ingredient_text[ingredient_text])
            pattern_rows.append("".join(pattern_characters))
        return tuple(pattern_rows), key

    @classmethod
    def _minecraft_recipe_mutation_from_form(
        cls,
        *,
        kind_value: str,
        recipe_id: str,
        output_item: str,
        output_count: str,
        ingredients_text: str,
        pattern_text: str,
        key_text: str,
    ) -> MinecraftRecipeMutation:
        kind = MinecraftRecipeKind(kind_value.strip())
        output = MinecraftRecipeItemStack(output_item, count=cls._parse_minecraft_recipe_count(output_count))
        normalised_recipe_id = recipe_id.strip() or None
        if kind is MinecraftRecipeKind.SHAPELESS:
            return MinecraftShapelessRecipe(
                output=output,
                ingredients=cls._parse_minecraft_recipe_ingredients(ingredients_text),
                recipe_id=normalised_recipe_id,
            )
        if kind is MinecraftRecipeKind.SHAPED:
            return MinecraftShapedRecipe(
                output=output,
                pattern=cls._parse_minecraft_recipe_pattern(pattern_text),
                key=cls._parse_minecraft_recipe_key(key_text),
                recipe_id=normalised_recipe_id,
            )
        if kind in (
            MinecraftRecipeKind.SMELTING,
            MinecraftRecipeKind.BLASTING,
            MinecraftRecipeKind.SMOKING,
            MinecraftRecipeKind.CAMPFIRE_COOKING,
        ):
            return MinecraftCookingRecipe(
                kind=kind,
                output=output,
                ingredient=cls._parse_single_minecraft_recipe_ingredient(ingredients_text),
                recipe_id=normalised_recipe_id,
            )
        if kind is MinecraftRecipeKind.STONECUTTING:
            return MinecraftStonecuttingRecipe(
                output=output,
                ingredient=cls._parse_single_minecraft_recipe_ingredient(ingredients_text),
                recipe_id=normalised_recipe_id,
            )
        raise ValueError(f"Unsupported recipe type: {kind.value}")

    @staticmethod
    def _parse_minecraft_recipe_count(raw_value: str) -> int:
        text = raw_value.strip()
        if not text:
            return 1
        if not text.isdecimal():
            raise ValueError("Recipe counts must be positive integers.")
        return int(text)

    @classmethod
    def _parse_minecraft_recipe_ingredients(cls, raw_value: str) -> tuple[MinecraftRecipeIngredient, ...]:
        parts = tuple(part.strip() for part in raw_value.replace("\n", ",").split(",") if part.strip())
        if not parts:
            raise ValueError("Enter at least one ingredient.")
        return tuple(cls._parse_single_minecraft_recipe_ingredient(part) for part in parts)

    @classmethod
    def _parse_single_minecraft_recipe_ingredient(cls, raw_value: str) -> MinecraftRecipeIngredient:
        del cls
        text = raw_value.strip()
        if not text:
            raise ValueError("Ingredient must not be empty.")
        count = 1
        if "x " in text.casefold():
            count_text, resource_text = text.split(" ", maxsplit=1)
            if count_text.casefold().endswith("x") and count_text[:-1].isdecimal():
                count = int(count_text[:-1])
                text = resource_text.strip()
        if text.startswith("#"):
            return MinecraftRecipeIngredient.tag(text[1:], count=count)
        return MinecraftRecipeIngredient.item(text, count=count)

    @classmethod
    def _parse_editor_ingredient_state_text(cls, raw_value: str) -> _MinecraftRecipeEditorIngredientState:
        text = raw_value.strip()
        if not text:
            return _MinecraftRecipeEditorIngredientState.empty()
        ingredient = cls._parse_single_minecraft_recipe_ingredient(text)
        return cls._minecraft_editor_ingredient_from_recipe_ingredient(ingredient)

    @classmethod
    def _minecraft_optional_recipe_ingredient_from_editor_state(
        cls,
        ingredient_state: _MinecraftRecipeEditorIngredientState,
    ) -> MinecraftRecipeIngredient | None:
        if not ingredient_state.has_value:
            return None
        return cls._parse_single_minecraft_recipe_ingredient(ingredient_state.editor_text)

    @classmethod
    def _parse_optional_minecraft_recipe_ingredient(cls, raw_value: str) -> MinecraftRecipeIngredient | None:
        if not raw_value.strip():
            return None
        return cls._parse_single_minecraft_recipe_ingredient(raw_value)

    @staticmethod
    def _parse_optional_minecraft_recipe_float(raw_value: str) -> float | None:
        text = raw_value.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError as xcp:
            raise ValueError("Recipe experience must be a non-negative number.") from xcp
        if value < 0:
            raise ValueError("Recipe experience must be a non-negative number.")
        return value

    @staticmethod
    def _parse_optional_minecraft_recipe_int(raw_value: str) -> int | None:
        text = raw_value.strip()
        if not text:
            return None
        if not text.isdecimal():
            raise ValueError("Cooking time must be a non-negative integer tick count.")
        return int(text)

    @staticmethod
    def _parse_optional_minecraft_recipe_type(raw_value: str) -> MinecraftRecipeKind | str | None:
        text = raw_value.strip()
        if not text:
            return None
        try:
            return MinecraftRecipeKind(text)
        except ValueError:
            return text

    @staticmethod
    def _parse_minecraft_recipe_pattern(raw_value: str) -> tuple[str, ...]:
        rows = tuple(row.rstrip() for row in raw_value.splitlines() if row.strip())
        if not rows:
            raise ValueError("Shaped recipes require a pattern.")
        return rows

    @classmethod
    def _parse_minecraft_recipe_key(cls, raw_value: str) -> dict[str, MinecraftRecipeIngredient]:
        key: dict[str, MinecraftRecipeIngredient] = {}
        for line in raw_value.splitlines():
            stripped_line = line.strip()
            if not stripped_line:
                continue
            if "=" not in stripped_line:
                raise ValueError("Shaped key entries must use SYMBOL=ingredient.")
            symbol, raw_ingredient = (part.strip() for part in stripped_line.split("=", maxsplit=1))
            key[symbol] = cls._parse_single_minecraft_recipe_ingredient(raw_ingredient)
        if not key:
            raise ValueError("Shaped recipes require a key.")
        return key

    @classmethod
    def _minecraft_recipes_body_markup(cls, summary: ModWebMinecraftRecipeBookSummary | None) -> str:
        if summary is None:
            return (
                '<div class="mod-card mod-card-empty mod-card-plain">'
                '<div class="mod-subtitle">Recipe book data is not available for this node yet.</div>'
                "</div>"
            )
        if summary.load_error is not None:
            return (
                '<div class="mod-card mod-card-empty mod-card-plain">'
                '<div class="mod-subtitle">Recipe book could not be loaded.</div>'
                f'<div class="mod-subtitle">{escape(summary.load_error)}</div>'
                "</div>"
            )
        if not summary.entries:
            return (
                '<div class="mod-card mod-card-empty mod-card-plain">'
                '<div class="mod-subtitle">No managed recipes yet.</div>'
                "</div>"
            )
        entries_markup = "".join(cls._minecraft_recipe_entry_markup(entry) for entry in summary.entries)
        return f'<div class="mod-recipe-list">{entries_markup}</div>'

    @staticmethod
    def _minecraft_item_registry_markup(summary: ModWebMinecraftItemRegistrySummary | None) -> str:
        if summary is None:
            return '<div class="mod-subtitle">Known item registry data is not available for this node yet.</div>'
        if summary.load_error is not None:
            return (
                f'<div class="mod-subtitle">Known item registry could not be loaded: {escape(summary.load_error)}</div>'
            )
        if not summary.file_exists:
            return (
                '<div class="mod-subtitle">'
                "Known item registry has not been generated yet. "
                "If Minecraft was already running before the bot wrote the managed KubeJS script, restart Minecraft "
                "again or run <code>/kubejs reload server_scripts</code>."
                "</div>"
            )
        generated_text = (
            ""
            if summary.generated_at_epoch_ms is None
            else f" Generated at <code>{summary.generated_at_epoch_ms}</code>."
        )
        return (
            '<div class="mod-subtitle">'
            f"Known item registry: <code>{escape(summary.data_path)}</code> with {len(summary.item_ids)} items."
            f"{generated_text}"
            "</div>"
        )

    @staticmethod
    def _minecraft_recipe_entry_markup(entry: ModWebMinecraftRecipeEntry) -> str:
        operation_label = "Add" if entry.operation is ModWebMinecraftRecipeOperationKind.ADD else "Remove"
        operation_class = (
            "mod-recipe-operation-add"
            if entry.operation is ModWebMinecraftRecipeOperationKind.ADD
            else "mod-recipe-operation-remove"
        )
        recipe_id_markup = (
            ""
            if entry.recipe_id is None
            else f'<div class="mod-subtitle">ID: <code>{escape(entry.recipe_id)}</code></div>'
        )
        return (
            '<div class="mod-card mod-card-plain mod-recipe-entry">'
            '<div class="mod-recipe-entry-head">'
            f'<span class="mod-recipe-operation {operation_class}">{escape(operation_label)}</span>'
            f'<span class="mod-recipe-kind">{escape(entry.kind_label)}</span>'
            "</div>"
            f'<div class="mod-title">{escape(entry.title)}</div>'
            f'<div class="mod-subtitle">{escape(entry.detail)}</div>'
            f"{recipe_id_markup}"
            "</div>"
        )
