"""Settings and console operations exposed by the node API."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Protocol, cast

from _security import Access_Control, Power_Level
from apps._app import App, AppStdoutTail
from apps._console import (
    ConsoleAction,
    ConsoleActionParameter,
    ConsoleActionResult,
    execute_console_action,
)
from apps._settings import Setting, Settings_Manager
from node_api_console import (
    NodeConsoleActionEntry,
    NodeConsoleActionExecutionResult,
    NodeConsoleActionList,
    NodeConsoleActionParameter,
    NodeConsoleStdoutSnapshot,
)
from node_api_route_contracts import HttpExceptionFactory
from node_api_settings import (
    NodeSettingChoice,
    NodeSettingEntry,
    NodeSettingList,
    NodeSettingMutationResult,
    NodeSettingsActionResult,
)


class RuntimeHttpExceptionFactory(Protocol):
    """Builds a client-safe HTTP exception for a runtime operation failure."""

    def __call__(self, *, app: App, action: str, error: RuntimeError) -> Exception: ...


class NodeAppOperationsService:
    """Owns per-app settings and console business operations."""

    def __init__(
        self,
        *,
        node_name: Callable[[], str],
        require_acl: Callable[[], Access_Control],
        http_exception: HttpExceptionFactory,
        runtime_http_exception: RuntimeHttpExceptionFactory,
        traffic_log: logging.Logger,
    ) -> None:
        self._node_name = node_name
        self._require_acl = require_acl
        self._http_exception = http_exception
        self._runtime_http_exception = runtime_http_exception
        self._traffic_log = traffic_log

    def build_setting_list(self, *, app: App, actor_user_id: int) -> NodeSettingList:
        settings = self._settings_for_app(app)
        acl = self._require_acl()
        settings_manager = self.require_settings_manager(app)
        entries = tuple(
            self._setting_entry(
                setting,
                acl=acl,
                actor_user_id=actor_user_id,
                settings_manager=settings_manager,
            )
            for setting in settings
        )
        editable_count = sum(
            1 for setting in settings if acl.can(actor_user_id, setting.power_level)
        )
        return NodeSettingList(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            editable_count=editable_count,
            restricted_count=len(settings) - editable_count,
            has_pending_changes=settings_manager.has_pending_changes(actor_user_id),
            pending_change_count=settings_manager.pending_change_count(actor_user_id),
            required_save_level_name=settings_manager.required_save_level(
                actor_user_id
            ).name,
            required_reload_level_name=settings_manager.required_reload_level(
                actor_user_id
            ).name,
            settings=entries,
        )

    async def update_setting(
        self,
        *,
        app: App,
        setting_key: str,
        value: str,
        actor_user_id: int,
    ) -> NodeSettingMutationResult:
        setting = self.resolve_setting(app=app, setting_key=setting_key)
        await self._require_acl().perm_check(actor_user_id, setting.power_level)
        settings_manager = self.require_settings_manager(app)

        resolved_value = value.strip()
        if not resolved_value and not setting.allows_blank_input:
            raise self._http_exception(400, "Setting value must not be empty.")

        try:
            settings_manager.update_setting(
                actor_user_id, setting, resolved_value, remember_input=True
            )
        except (IndexError, ValueError) as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        except Exception as xcp:
            raise self._http_exception(500, f"Setting update failed: {xcp}") from xcp

        self._traffic_log.info(
            "Node API setting updated: node=%s app=%s setting=%s actor=%s",
            self._node_name(),
            app.name,
            setting.key,
            actor_user_id,
        )
        return NodeSettingMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            setting_key=setting.key,
            message=(
                f"{app.friendly} setting `{setting.label}` updated: "
                f"{settings_manager.display_value(setting, actor_user_id)}. "
                "Settings are saved on launch or via Save Settings."
            ),
            setting=self._setting_entry(
                setting,
                acl=self._require_acl(),
                actor_user_id=actor_user_id,
                settings_manager=settings_manager,
            ),
        )

    async def save_settings(
        self, *, app: App, actor_user_id: int
    ) -> NodeSettingsActionResult:
        settings_manager = self.require_settings_manager(app)
        await self._require_acl().perm_check(
            actor_user_id, settings_manager.required_save_level(actor_user_id)
        )
        try:
            settings_manager.save(actor_user_id)
        except Exception as xcp:
            raise self._http_exception(500, f"Settings save failed: {xcp}") from xcp
        self._traffic_log.info(
            "Node API settings saved: node=%s app=%s actor=%s",
            self._node_name(),
            app.name,
            actor_user_id,
        )
        return NodeSettingsActionResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            message=f"Saved settings for {app.friendly}.",
        )

    async def reload_settings(
        self, *, app: App, actor_user_id: int
    ) -> NodeSettingsActionResult:
        settings_manager = self.require_settings_manager(app)
        await self._require_acl().perm_check(
            actor_user_id, settings_manager.required_reload_level(actor_user_id)
        )
        try:
            settings_manager.load(actor_user_id)
        except Exception as xcp:
            raise self._http_exception(500, f"Settings reload failed: {xcp}") from xcp
        self._traffic_log.info(
            "Node API settings reloaded: node=%s app=%s actor=%s",
            self._node_name(),
            app.name,
            actor_user_id,
        )
        return NodeSettingsActionResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            message=f"{app.friendly} settings reloaded from disk.",
        )

    def build_console_action_list(
        self, *, app: App, actor_user_id: int
    ) -> NodeConsoleActionList:
        acl = self._require_acl()
        runtime_running = app.check_running()
        return NodeConsoleActionList(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            actions=tuple(
                self._console_action_entry(
                    action=action,
                    actor_user_id=actor_user_id,
                    acl=acl,
                    runtime_running=runtime_running,
                )
                for action in app.console_actions
            ),
        )

    async def read_console_stdout(
        self,
        *,
        app: App,
        actor_user_id: int,
        max_lines: int = 200,
    ) -> NodeConsoleStdoutSnapshot:
        await self._require_acl().perm_check(actor_user_id, Power_Level.user)
        return self.build_console_stdout_snapshot(app=app, max_lines=max_lines)

    def build_console_stdout_snapshot(
        self,
        *,
        app: App,
        max_lines: int = 200,
    ) -> NodeConsoleStdoutSnapshot:
        try:
            stdout_tail: AppStdoutTail = app.read_stdout_tail(max_lines=max_lines)
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        except Exception as xcp:
            raise self._http_exception(
                500, f"Console stdout read failed: {xcp}"
            ) from xcp
        return NodeConsoleStdoutSnapshot(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            lines=stdout_tail.lines,
            truncated=stdout_tail.truncated,
            running=app.check_running(),
        )

    async def execute_console_action(
        self,
        *,
        app: App,
        action_key: str,
        raw_value: str | None,
        actor_user_id: int,
    ) -> NodeConsoleActionExecutionResult:
        action = self.resolve_console_action(app, action_key)
        await self._require_acl().perm_check(actor_user_id, action.power_level)
        try:
            result: ConsoleActionResult = await execute_console_action(
                app=app,
                is_running=app.check_running,
                action=action,
                raw_value=raw_value,
            )
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        except RuntimeError as xcp:
            raise self._runtime_http_exception(
                app=app, action="Console action", error=xcp
            ) from xcp
        except Exception as xcp:
            raise self._http_exception(500, f"Console action failed: {xcp}") from xcp

        self._traffic_log.info(
            "Node API console action executed: node=%s app=%s action=%s actor=%s success=%s",
            self._node_name(),
            app.name,
            action.key,
            actor_user_id,
            result.success,
        )
        return NodeConsoleActionExecutionResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            action_key=action.key,
            summary=result.summary,
            success=result.success,
            text=result.text,
            source=result.source,
        )

    def resolve_console_action(self, app: App, action_key: str) -> ConsoleAction:
        if not app.supports_console_actions:
            raise self._http_exception(
                404, f"{app.friendly} does not support console actions."
            )
        normalised_key = action_key.strip().casefold()
        if not normalised_key:
            raise self._http_exception(400, "Console action key must not be empty.")
        for action in app.console_actions:
            if action.key.casefold() == normalised_key:
                return action
        raise self._http_exception(404, f"Unknown console action: {action_key}")

    def _settings_for_app(self, app: App) -> tuple[Setting[object], ...]:
        settings_manager = self.require_settings_manager(app)
        return tuple(cast(Sequence[Setting[object]], settings_manager.app.options))

    def resolve_setting(self, *, app: App, setting_key: str) -> Setting[object]:
        setting = self._setting_lookup(app).get(setting_key.casefold())
        if setting is None:
            raise self._http_exception(404, f"Unknown setting: {setting_key}")
        return setting

    def _setting_lookup(self, app: App) -> dict[str, Setting[object]]:
        return {
            setting.key.casefold(): setting for setting in self._settings_for_app(app)
        }

    def require_settings_manager(self, app: App) -> Settings_Manager:
        settings_manager = app.settings
        if settings_manager is None:
            raise self._http_exception(
                404, f"{app.friendly} does not support settings."
            )
        return settings_manager

    @staticmethod
    def _setting_current_input_value(
        setting: Setting[object],
        *,
        can_edit: bool,
        settings_manager: Settings_Manager,
        actor_user_id: int,
    ) -> str:
        if setting.do_hide is not None and (
            not can_edit or setting.value_type is not bool
        ):
            return ""
        return settings_manager.current_input_value(setting, actor_user_id)

    @staticmethod
    def _setting_recent_inputs(setting: Setting[object]) -> tuple[str, ...]:
        return setting.recent_inputs if setting.supports_recent_inputs else ()

    @staticmethod
    def _setting_allows_text_input(setting: Setting[object]) -> bool:
        return not setting.choices or not setting.strict_choice

    @staticmethod
    def _setting_label_text(setting: Setting[object], value: object) -> str:
        choice_label = setting.spec.choice_label_for_value(value)
        return (
            choice_label
            if choice_label is not None
            else setting.spec.display_value(value)
        )

    @classmethod
    def _setting_default_text(cls, setting: Setting[object]) -> str:
        return (
            ""
            if setting.do_hide is not None
            else cls._setting_label_text(setting, setting.default)
        )

    @staticmethod
    def _setting_value_text(
        setting: Setting[object],
        *,
        can_reveal: bool,
        settings_manager: Settings_Manager,
        actor_user_id: int,
    ) -> str:
        if setting.do_hide is None:
            return settings_manager.label_text(setting, actor_user_id)
        if setting.is_sensitive:
            return "REDACTED"
        if can_reveal:
            return "Hidden"
        return f"Hidden (requires {setting.do_hide.name.title()})"

    @staticmethod
    def _setting_revealed_value_text(
        setting: Setting[object],
        *,
        can_reveal: bool,
        settings_manager: Settings_Manager,
        actor_user_id: int,
    ) -> str:
        if setting.do_hide is None or not can_reveal:
            return ""
        return settings_manager.label_text(setting, actor_user_id)

    @classmethod
    def _setting_entry(
        cls,
        setting: Setting[object],
        *,
        acl: Access_Control,
        actor_user_id: int,
        settings_manager: Settings_Manager,
    ) -> NodeSettingEntry:
        can_edit = acl.can(actor_user_id, setting.power_level)
        reveal_level = setting.do_hide
        can_reveal = reveal_level is not None and acl.can(actor_user_id, reveal_level)
        return NodeSettingEntry(
            key=setting.key,
            label=setting.label,
            type_name=setting.type_name,
            permission_level=setting.power_level.name.title(),
            permission_level_name=setting.power_level.name,
            default_text=cls._setting_default_text(setting),
            description=setting.desc,
            paragraph=setting.paragraph,
            is_sensitive=setting.is_sensitive,
            value_text=cls._setting_value_text(
                setting,
                can_reveal=can_reveal,
                settings_manager=settings_manager,
                actor_user_id=actor_user_id,
            ),
            revealed_value_text=cls._setting_revealed_value_text(
                setting,
                can_reveal=can_reveal,
                settings_manager=settings_manager,
                actor_user_id=actor_user_id,
            ),
            current_input_value=cls._setting_current_input_value(
                setting,
                can_edit=can_edit,
                settings_manager=settings_manager,
                actor_user_id=actor_user_id,
            ),
            has_pending_value=settings_manager.has_pending_value(
                actor_user_id, setting
            ),
            can_edit=can_edit,
            value_is_hidden=reveal_level is not None,
            can_reveal_hidden_text=can_reveal,
            allows_text_input=cls._setting_allows_text_input(setting),
            allows_blank_input=setting.allows_blank_input,
            strict_choice=setting.strict_choice,
            choices=tuple(
                NodeSettingChoice(label=label, raw_value=raw_value)
                for label, raw_value in setting.choice_items()
            ),
            recent_inputs=cls._setting_recent_inputs(setting) if can_edit else (),
            group_id=setting.group.name if setting.group is not None else None,
            group_label=setting.group.value if setting.group is not None else None,
        )

    def _console_action_entry(
        self,
        *,
        action: ConsoleAction,
        actor_user_id: int,
        acl: Access_Control,
        runtime_running: bool,
    ) -> NodeConsoleActionEntry:
        parameter = action.parameter
        can_run = acl.can(actor_user_id, action.power_level)
        return NodeConsoleActionEntry(
            key=action.key,
            label=action.label,
            description=action.description,
            power_level_name=action.power_level.name,
            power_level_label=action.power_level.name.title(),
            requires_running=action.requires_running,
            can_run=can_run,
            runtime_running=runtime_running,
            parameter=self._console_action_parameter_entry(
                parameter, include_recent_inputs=can_run
            )
            if parameter is not None
            else None,
        )

    @staticmethod
    def _console_action_parameter_entry(
        parameter: ConsoleActionParameter[object],
        *,
        include_recent_inputs: bool,
    ) -> NodeConsoleActionParameter:
        return NodeConsoleActionParameter(
            key=parameter.key,
            label=parameter.label,
            value_type_name=parameter.value_type_name,
            description=parameter.desc,
            max_length=parameter.max_length,
            multiline=parameter.multiline,
            strict_choice=parameter.strict_choice,
            allows_text_input=parameter.choice_spec is None
            or not parameter.strict_choice,
            choices=tuple(
                NodeSettingChoice(label=label, raw_value=raw_value)
                for label, raw_value in parameter.choice_items()
            ),
            recent_inputs=parameter.recent_inputs if include_recent_inputs else (),
        )
