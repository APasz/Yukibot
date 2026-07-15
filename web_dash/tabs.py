from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .runtime_imports import BadgeTone, replace
from .service_base import ModWebServiceSupport
from .types import (
    ModWebAppLink,
    ModWebAppSectionKind,
    ModWebAppTabContext,
    ModWebAppTabDefinition,
    ModWebBasePageModel,
    ModWebPageModel,
)

_VERSION_TOKEN_RE: re.Pattern[str] = re.compile(r"\d+|[A-Za-z]+")
_MINECRAFT_APP_SCOPE = "minecraft"
_SEVENDAYS_APP_SCOPE = "sevendays"
_SEVENDAYS_SANDBOX_OPTIONS_MIN_VERSION_TEXT = "3.0.259"
_KUBEJS_MOD_BASE_NAME = "kubejs"
_KUBEJS_LOADER_TOKENS: frozenset[str] = frozenset({"forge", "fabric", "quilt", "neoforge"})
_KUBEJS_RECIPE_ADDON_LABELS: dict[str, str] = {
    "kubejs-create": "KubeJS Create",
    "kubejs-immersive-engineering": "KubeJS Immersive Engineering",
    "kubejs-immersiveengineering": "KubeJS Immersive Engineering",
}


@dataclass(frozen=True, slots=True)
class _BuiltinTabPresentation:
    page_order: int
    app_card_order: int
    app_card_tone: BadgeTone
    icon: str


_BUILTIN_TAB_PRESENTATIONS: dict[ModWebAppSectionKind, _BuiltinTabPresentation] = {
    ModWebAppSectionKind.UPDATE: _BuiltinTabPresentation(150, 350, "grey", "system_update_alt"),
    ModWebAppSectionKind.MODS: _BuiltinTabPresentation(100, 500, "purple", "extension"),
    ModWebAppSectionKind.CONFIGS: _BuiltinTabPresentation(200, 200, "grey", "description"),
    ModWebAppSectionKind.SETTINGS: _BuiltinTabPresentation(300, 300, "grey", "tune"),
    ModWebAppSectionKind.SAVES: _BuiltinTabPresentation(400, 100, "grey", "save"),
    ModWebAppSectionKind.CONSOLE: _BuiltinTabPresentation(500, 400, "grey", "terminal"),
    ModWebAppSectionKind.CHAT: _BuiltinTabPresentation(600, 600, "purple", "forum"),
}


class ModWebTabsMixin(ModWebServiceSupport):
    def _page_tabs(self, model: ModWebBasePageModel) -> tuple[ModWebAppTabDefinition, ...]:
        if model.tabs:
            return self._sorted_page_tabs(model.tabs)
        return self._resolved_page_tabs(model)

    def _app_link_tabs(self, app: ModWebAppLink) -> tuple[ModWebAppTabDefinition, ...]:
        if app.tabs:
            return self._sorted_app_card_tabs(app.tabs)
        return self._resolved_app_link_tabs(app)

    def _page_model_with_tabs(self, model: ModWebBasePageModel) -> ModWebBasePageModel:
        return replace(model, tabs=self._resolved_page_tabs(model))

    def _app_link_with_tabs(self, app: ModWebAppLink) -> ModWebAppLink:
        return replace(app, tabs=self._resolved_app_link_tabs(app))

    def _resolved_page_tabs(self, model: ModWebBasePageModel) -> tuple[ModWebAppTabDefinition, ...]:
        context: ModWebAppTabContext = self._page_tab_context(model)
        definitions: tuple[ModWebAppTabDefinition, ...] = (
            self._built_in_page_tab_definitions(model) + self._additional_app_tab_definitions(context=context, is_detail_page=True)
        )
        return self._sorted_page_tabs(definitions)

    def _resolved_app_link_tabs(self, app: ModWebAppLink) -> tuple[ModWebAppTabDefinition, ...]:
        context: ModWebAppTabContext = self._app_link_tab_context(app)
        definitions: tuple[ModWebAppTabDefinition, ...] = (
            self._built_in_app_link_tab_definitions(app)
            + self._additional_app_tab_definitions(context=context, is_detail_page=False)
        )
        return self._sorted_app_card_tabs(definitions)

    def _additional_app_tab_definitions(
        self,
        *,
        context: ModWebAppTabContext,
        is_detail_page: bool,
    ) -> tuple[ModWebAppTabDefinition, ...]:
        definitions: list[ModWebAppTabDefinition] = []
        if context.supports_map:
            definitions.append(
                ModWebAppTabDefinition.custom(
                    tab_id="map",
                    label="Map",
                    page_order=350,
                    app_card_order=150,
                    app_card_tone="purple",
                    icon="map",
                    render_handler_name="_render_map_section",
                    badge_handler_name="_map_tab_badges",
                    action_handler_name="_map_tab_actions",
                )
            )
        if context.supports_blueprints:
            definitions.append(
                ModWebAppTabDefinition.custom(
                    tab_id="blueprints",
                    label="Blueprints",
                    page_order=450,
                    app_card_order=650,
                    app_card_tone="grey",
                    icon="account_tree",
                    show_on_app_card=False,
                    render_handler_name="_render_blueprints_section",
                    badge_handler_name="_blueprint_tab_badges",
                    app_card_badge_handler_name="_blueprint_app_card_badges",
                )
            )
        if is_detail_page and self._minecraft_recipes_tab_available(context):
            definitions.append(
                ModWebAppTabDefinition.custom(
                    tab_id="recipes",
                    label="Recipes",
                    page_order=425,
                    app_card_order=675,
                    app_card_tone="grey",
                    icon="menu_book",
                    show_on_app_card=False,
                    render_handler_name="_render_minecraft_recipes_section",
                    badge_handler_name="_minecraft_recipes_tab_badges",
                )
            )
        if is_detail_page and self._sevendays_sandbox_options_tab_available(context):
            definitions.append(
                ModWebAppTabDefinition.custom(
                    tab_id="sandbox",
                    label="Sandbox",
                    page_order=475,
                    app_card_order=675,
                    app_card_tone="grey",
                    icon="sports_esports",
                    show_on_app_card=False,
                    render_handler_name="_render_sevendays_sandbox_options_section",
                    badge_handler_name="_sevendays_sandbox_options_tab_badges",
                )
            )
        return tuple(definitions)

    @staticmethod
    def _page_tab_context(model: ModWebBasePageModel) -> ModWebAppTabContext:
        if isinstance(model, ModWebPageModel):
            enabled_mod_names = tuple(mod.name for mod in model.mods.mods if mod.enabled)
        else:
            enabled_mod_names = ()
        return ModWebAppTabContext(
            app_name=model.app_name,
            app_version=(
                model.app_stats.version
                if model.app_stats is not None and model.app_stats.version is not None
                else (
                    None
                    if model.sevendays_sandbox_options is None
                    else model.sevendays_sandbox_options.app_version
                )
            ),
            app_scope=model.app_scope,
            enabled_mod_names=enabled_mod_names,
            supports_map=model.map_api_url is not None,
            supports_blueprints=model.blueprints is not None,
            supports_sevendays_sandbox_options=model.sevendays_sandbox_options is not None,
        )

    @staticmethod
    def _app_link_tab_context(app: ModWebAppLink) -> ModWebAppTabContext:
        return ModWebAppTabContext(
            app_name=app.name,
            app_scope=app.app_scope,
            supports_map=app.map_url is not None,
            supports_blueprints=app.supports_blueprints,
        )

    @classmethod
    def _minecraft_recipes_tab_available(cls, context: ModWebAppTabContext) -> bool:
        return cls._app_scope_matches(context, _MINECRAFT_APP_SCOPE) and cls._has_enabled_kubejs(context)

    @staticmethod
    def _resolved_context_scope(context: ModWebAppTabContext) -> str:
        if context.app_scope is not None and context.app_scope.strip():
            return context.app_scope.strip().casefold()
        return context.app_name.strip().split("_", maxsplit=1)[0].casefold()

    @classmethod
    def _app_scope_matches(cls, context: ModWebAppTabContext, expected_scope: str) -> bool:
        return cls._resolved_context_scope(context) == expected_scope

    @classmethod
    def _sevendays_sandbox_options_tab_available(cls, context: ModWebAppTabContext) -> bool:
        if not context.supports_sevendays_sandbox_options:
            return False
        if not cls._app_scope_matches(context, _SEVENDAYS_APP_SCOPE):
            return False
        if context.app_version is None:
            return False
        return cls._app_version_is_at_least(context.app_version, _SEVENDAYS_SANDBOX_OPTIONS_MIN_VERSION_TEXT)

    @classmethod
    def _has_enabled_kubejs(cls, context: ModWebAppTabContext) -> bool:
        return any(cls._is_kubejs_mod_name(mod_name) for mod_name in context.enabled_mod_names)

    @staticmethod
    def _normalised_minecraft_mod_stem(name: str) -> str:
        return Path(name).stem.strip().casefold().replace("_", "-")

    @classmethod
    def _is_kubejs_mod_name(cls, name: str) -> bool:
        stem: str = cls._normalised_minecraft_mod_stem(name)
        if stem == _KUBEJS_MOD_BASE_NAME:
            return True
        tokens: tuple[str, ...] = tuple(token for token in stem.split("-") if token)
        return len(tokens) >= 2 and tokens[0] == _KUBEJS_MOD_BASE_NAME and tokens[1] in _KUBEJS_LOADER_TOKENS

    @classmethod
    def _kubejs_recipe_addon_labels(cls, mod_names: tuple[str, ...]) -> tuple[str, ...]:
        labels: list[str] = []
        seen_labels: set[str] = set()
        for mod_name in mod_names:
            addon_label: str | None = cls._kubejs_recipe_addon_label(mod_name)
            if addon_label is None:
                continue
            label_key: str = addon_label.casefold()
            if label_key in seen_labels:
                continue
            labels.append(addon_label)
            seen_labels.add(label_key)
        return tuple(labels)

    @classmethod
    def _kubejs_recipe_addon_label(cls, name: str) -> str | None:
        stem: str = cls._normalised_minecraft_mod_stem(name)
        for addon_id, label in _KUBEJS_RECIPE_ADDON_LABELS.items():
            if stem == addon_id or stem.startswith(f"{addon_id}-"):
                return label
        return None

    def _built_in_page_tab_definitions(self, model: ModWebBasePageModel) -> tuple[ModWebAppTabDefinition, ...]:
        definitions: list[ModWebAppTabDefinition] = []
        if model.supports_updates:
            definitions.append(self._builtin_tab_definition(ModWebAppSectionKind.UPDATE))
        if isinstance(model, ModWebPageModel):
            definitions.append(self._builtin_tab_definition(ModWebAppSectionKind.MODS))
        if model.supports_configs:
            definitions.append(self._builtin_tab_definition(ModWebAppSectionKind.CONFIGS))
        if model.settings is not None:
            definitions.append(self._builtin_tab_definition(ModWebAppSectionKind.SETTINGS))
        if model.saves is not None:
            definitions.append(self._builtin_tab_definition(ModWebAppSectionKind.SAVES))
        if model.console_actions is not None:
            definitions.append(self._builtin_tab_definition(ModWebAppSectionKind.CONSOLE))
        if model.supports_chat:
            definitions.append(self._builtin_tab_definition(ModWebAppSectionKind.CHAT))
        return tuple(definitions)

    def _built_in_app_link_tab_definitions(self, app: ModWebAppLink) -> tuple[ModWebAppTabDefinition, ...]:
        definitions: list[ModWebAppTabDefinition] = []
        if app.supports_updates:
            definitions.append(self._builtin_tab_definition(ModWebAppSectionKind.UPDATE))
        if app.supports_saves:
            definitions.append(self._builtin_tab_definition(ModWebAppSectionKind.SAVES))
        if app.supports_configs:
            definitions.append(self._builtin_tab_definition(ModWebAppSectionKind.CONFIGS))
        if app.supports_settings:
            definitions.append(self._builtin_tab_definition(ModWebAppSectionKind.SETTINGS))
        if app.supports_console_actions:
            definitions.append(self._builtin_tab_definition(ModWebAppSectionKind.CONSOLE))
        if app.supports_mods:
            definitions.append(self._builtin_tab_definition(ModWebAppSectionKind.MODS))
        if app.supports_chat:
            definitions.append(self._builtin_tab_definition(ModWebAppSectionKind.CHAT))
        return tuple(definitions)

    @staticmethod
    def _builtin_tab_definition(section_kind: ModWebAppSectionKind) -> ModWebAppTabDefinition:
        presentation: _BuiltinTabPresentation = _BUILTIN_TAB_PRESENTATIONS[section_kind]
        return ModWebAppTabDefinition.builtin(
            builtin_kind=section_kind,
            page_order=presentation.page_order,
            app_card_order=presentation.app_card_order,
            app_card_tone=presentation.app_card_tone,
            icon=presentation.icon,
        )

    @staticmethod
    def _validate_unique_tab_ids(definitions: tuple[ModWebAppTabDefinition, ...]) -> None:
        seen_tab_ids: set[str] = set()
        for definition in definitions:
            tab_key: str = definition.tab_id.casefold()
            if tab_key in seen_tab_ids:
                raise ValueError(f"Duplicate app tab id: {definition.tab_id}")
            seen_tab_ids.add(tab_key)

    @staticmethod
    def _sorted_page_tabs(definitions: tuple[ModWebAppTabDefinition, ...]) -> tuple[ModWebAppTabDefinition, ...]:
        ModWebTabsMixin._validate_unique_tab_ids(definitions)
        return tuple(
            sorted(
                definitions,
                key=lambda definition: (
                    definition.page_order,
                    definition.label.casefold(),
                    definition.tab_id.casefold(),
                ),
            )
        )

    @staticmethod
    def _sorted_app_card_tabs(definitions: tuple[ModWebAppTabDefinition, ...]) -> tuple[ModWebAppTabDefinition, ...]:
        ModWebTabsMixin._validate_unique_tab_ids(definitions)
        return tuple(
            sorted(
                definitions,
                key=lambda definition: (
                    definition.app_card_order,
                    definition.label.casefold(),
                    definition.tab_id.casefold(),
                ),
            )
        )

    @classmethod
    def _app_version_is_at_least(cls, current_version: str, minimum_version: str) -> bool:
        current_tokens: tuple[str, ...] = cls._version_tokens(current_version)
        minimum_tokens: tuple[str, ...] = cls._version_tokens(minimum_version)
        for current_token, minimum_token in zip(current_tokens, minimum_tokens, strict=False):
            comparison: int = cls._compare_version_tokens(current_token, minimum_token)
            if comparison > 0:
                return True
            if comparison < 0:
                return False
        return len(current_tokens) >= len(minimum_tokens)

    @staticmethod
    def _version_tokens(version_text: str) -> tuple[str, ...]:
        stripped_text: str = version_text.strip()
        if not stripped_text:
            return ()
        matched_tokens: tuple[str, ...] = tuple(_VERSION_TOKEN_RE.findall(stripped_text))
        if matched_tokens:
            return ModWebTabsMixin._normalised_version_tokens(stripped_text, matched_tokens)
        return (stripped_text.casefold(),)

    @staticmethod
    def _normalised_version_tokens(version_text: str, tokens: tuple[str, ...]) -> tuple[str, ...]:
        if ":" not in version_text or len(tokens) < 4 or not all(token.isdigit() for token in tokens):
            return tokens
        normalised_tokens: list[str] = list(tokens)
        build_prefix_index = len(normalised_tokens) - 2
        if normalised_tokens[build_prefix_index] != "0":
            return tokens
        del normalised_tokens[build_prefix_index]
        return tuple(normalised_tokens)

    @staticmethod
    def _compare_version_tokens(current_token: str, minimum_token: str) -> int:
        current_is_digit: bool = current_token.isdigit()
        minimum_is_digit: bool = minimum_token.isdigit()
        if current_is_digit and minimum_is_digit:
            current_value: int = int(current_token)
            minimum_value: int = int(minimum_token)
            return (current_value > minimum_value) - (current_value < minimum_value)
        if current_is_digit != minimum_is_digit:
            return 1 if current_is_digit else -1
        current_text: str = current_token.casefold()
        minimum_text: str = minimum_token.casefold()
        return (current_text > minimum_text) - (current_text < minimum_text)
