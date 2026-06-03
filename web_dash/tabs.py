from __future__ import annotations

import re

from .runtime_imports import NodeSettingEntry, replace
from .service_base import ModWebServiceSupport
from .types import (
    ModWebAppLink,
    ModWebAppSectionKind,
    ModWebAppTabContext,
    ModWebAppTabDefinition,
    ModWebAppTabSettingSnapshot,
    ModWebAppTabVisibilityKind,
    ModWebAppTabVisibilityRule,
    ModWebBasePageModel,
    ModWebPageModel,
)

_VERSION_TOKEN_RE: re.Pattern[str] = re.compile(r"\d+|[A-Za-z]+")
_ENABLED_SETTING_TEXTS: frozenset[str] = frozenset({"1", "true", "yes", "on", "enabled"})


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
        return self._resolve_visible_app_tabs(definitions=definitions, context=context, sort_for="page")

    def _resolved_app_link_tabs(self, app: ModWebAppLink) -> tuple[ModWebAppTabDefinition, ...]:
        context: ModWebAppTabContext = self._app_link_tab_context(app)
        definitions: tuple[ModWebAppTabDefinition, ...] = (
            self._built_in_app_link_tab_definitions(app)
            + self._additional_app_tab_definitions(context=context, is_detail_page=False)
        )
        return self._resolve_visible_app_tabs(definitions=definitions, context=context, sort_for="app_card")

    def _additional_app_tab_definitions(
        self,
        *,
        context: ModWebAppTabContext,
        is_detail_page: bool,
    ) -> tuple[ModWebAppTabDefinition, ...]:
        """Override to register extra app tabs without touching the shared tab renderer."""
        del context, is_detail_page
        return ()

    @staticmethod
    def _page_tab_context(model: ModWebBasePageModel) -> ModWebAppTabContext:
        mod_names: tuple[str, ...]
        if isinstance(model, ModWebPageModel):
            mod_names = tuple(mod.name for mod in model.mods.mods)
        else:
            mod_names = ()
        settings: tuple[ModWebAppTabSettingSnapshot, ...]
        if model.settings is None:
            settings = ()
        else:
            settings = tuple(
                ModWebAppTabSettingSnapshot(key=setting.key, value_text=ModWebTabsMixin._setting_snapshot_text(setting))
                for setting in model.settings.settings
            )
        return ModWebAppTabContext(
            app_name=model.app_name,
            app_friendly=model.app_friendly,
            app_version=model.app_stats.version if model.app_stats is not None else None,
            mod_names=mod_names,
            settings=settings,
        )

    @staticmethod
    def _app_link_tab_context(app: ModWebAppLink) -> ModWebAppTabContext:
        return ModWebAppTabContext(app_name=app.name, app_friendly=app.friendly)

    @staticmethod
    def _setting_snapshot_text(setting: NodeSettingEntry) -> str:
        return setting.value_text

    def _built_in_page_tab_definitions(self, model: ModWebBasePageModel) -> tuple[ModWebAppTabDefinition, ...]:
        definitions: list[ModWebAppTabDefinition] = []
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
        if section_kind is ModWebAppSectionKind.MODS:
            return ModWebAppTabDefinition.builtin(
                builtin_kind=section_kind,
                page_order=100,
                app_card_order=500,
                app_card_tone="purple",
            )
        if section_kind is ModWebAppSectionKind.CONFIGS:
            return ModWebAppTabDefinition.builtin(
                builtin_kind=section_kind,
                page_order=200,
                app_card_order=200,
                app_card_tone="black",
            )
        if section_kind is ModWebAppSectionKind.SETTINGS:
            return ModWebAppTabDefinition.builtin(
                builtin_kind=section_kind,
                page_order=300,
                app_card_order=300,
                app_card_tone="black",
            )
        if section_kind is ModWebAppSectionKind.SAVES:
            return ModWebAppTabDefinition.builtin(
                builtin_kind=section_kind,
                page_order=400,
                app_card_order=100,
                app_card_tone="black",
            )
        if section_kind is ModWebAppSectionKind.CONSOLE:
            return ModWebAppTabDefinition.builtin(
                builtin_kind=section_kind,
                page_order=500,
                app_card_order=400,
                app_card_tone="black",
            )
        return ModWebAppTabDefinition.builtin(
            builtin_kind=section_kind,
            page_order=600,
            app_card_order=600,
            app_card_tone="purple",
        )

    def _resolve_visible_app_tabs(
        self,
        *,
        definitions: tuple[ModWebAppTabDefinition, ...],
        context: ModWebAppTabContext,
        sort_for: str,
    ) -> tuple[ModWebAppTabDefinition, ...]:
        self._validate_unique_tab_ids(definitions)
        visible_tabs: tuple[ModWebAppTabDefinition, ...] = tuple(
            definition for definition in definitions if self._app_tab_visibility_matches(definition.visibility_rule, context)
        )
        if sort_for == "page":
            return self._sorted_page_tabs(visible_tabs)
        if sort_for == "app_card":
            return self._sorted_app_card_tabs(visible_tabs)
        raise ValueError(f"Unsupported app tab sort target: {sort_for}")

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

    def _app_tab_visibility_matches(
        self,
        rule: ModWebAppTabVisibilityRule,
        context: ModWebAppTabContext,
    ) -> bool:
        if rule.kind is ModWebAppTabVisibilityKind.ALWAYS:
            return True
        if rule.kind is ModWebAppTabVisibilityKind.MIN_APP_VERSION:
            if context.app_version is None:
                return False
            if rule.app_version is None:
                raise ValueError("Minimum-version app tab rule unexpectedly missing its version.")
            return self._app_version_is_at_least(context.app_version, rule.app_version)
        if rule.kind is ModWebAppTabVisibilityKind.HAS_MOD:
            if rule.mod_name is None:
                raise ValueError("Mod-gated app tab rule unexpectedly missing its mod name.")
            return context.has_mod(rule.mod_name)
        if rule.kind is ModWebAppTabVisibilityKind.SETTING_ENABLED:
            if rule.setting_key is None:
                raise ValueError("Setting-enabled app tab rule unexpectedly missing its setting key.")
            setting_value: str | None = context.setting_value(rule.setting_key)
            if setting_value is None:
                return False
            return self._setting_text_is_enabled(setting_value)
        if rule.kind is ModWebAppTabVisibilityKind.SETTING_EQUALS:
            if rule.setting_key is None:
                raise ValueError("Setting-equals app tab rule unexpectedly missing its setting key.")
            if rule.setting_value is None:
                raise ValueError("Setting-equals app tab rule unexpectedly missing its expected value.")
            setting_value = context.setting_value(rule.setting_key)
            if setting_value is None:
                return False
            return setting_value.strip().casefold() == rule.setting_value.strip().casefold()
        if rule.kind is ModWebAppTabVisibilityKind.ALL:
            return all(self._app_tab_visibility_matches(child_rule, context) for child_rule in rule.children)
        return any(self._app_tab_visibility_matches(child_rule, context) for child_rule in rule.children)

    @staticmethod
    def _setting_text_is_enabled(setting_value: str) -> bool:
        return setting_value.strip().casefold() in _ENABLED_SETTING_TEXTS

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
            return matched_tokens
        return (stripped_text.casefold(),)

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
