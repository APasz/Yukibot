from __future__ import annotations

from typing import TYPE_CHECKING

from modmux.models import Provider

from apps._config import (
    CurseForgeFileReference,
    LauncherProviderUrls,
    launcher_provider_label,
    mod_capabilities_for_scope,
)
from apps._launcher_metadata import has_curseforge_api_key
from apps.minecraft import MinecraftRecipeMutation

from .constants import (
    _DOWNLOAD_FEEDBACK_DELAY_SECONDS,
    _REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
    log,
)
from .nicegui_protocols import ModWebUi, _value_as_object, _value_as_text
from .runtime_imports import (
    Awaitable,
    BadgeTone,
    Callable,
    Checkbox,
    ClientPackConfig,
    ClientPackPolicy,
    Input,
    Literal,
    ModDownloadBlockReason,
    ModMetadataOverrides,
    ModPlacement,
    ModType,
    ModWebUser,
    NodeApiScope,
    NodeAppMutationAction,
    NodeAppMutationResult,
    NodeAppRuntimeSummary,
    NodeAppTransitionState,
    NodeCapacityMutationResult,
    NodeDiskManagementState,
    NodeDiskSettingsMutationResult,
    NodeFontSourceSettingsMutationResult,
    NodeMinecraftRecipeMutationAction,
    NodeMinecraftRecipeMutationResult,
    NodeModEntry,
    NodeModMutationAction,
    NodeModMutationResult,
    NodeSystemAction,
    NodeSystemActionResult,
    NodeRestartScheduleState,
    RestartTarget,
    Power_Level,
    assert_never,
    asyncio,
    config,
    datetime,
    quote,
    required_app_mutation_level,
    required_app_mutation_scope,
    required_mod_mutation_level,
)
from .service_base import ModWebServiceSupport
from .types import (
    ModDownloadKind,
    ModWebBasePageModel,
    ModWebNodeLink,
    ModWebPageModel,
    _ModWebKillControlState,
    _ModWebStartStopControlState,
)

if TYPE_CHECKING:
    from nicegui.elements.dialog import Dialog
    from nicegui.events import ValueChangeEventArguments


class ModWebActionsMixin(ModWebServiceSupport):
    @staticmethod
    def _mod_type_badge_tone(mod_type: ModType) -> BadgeTone:
        match mod_type:
            case ModType.REGULAR:
                return "grey"
            case ModType.CLIENT:
                return "purple"
            case ModType.SERVER:
                return "warn"
            case ModType.COREMOD:
                return "red"
            case ModType.BUILTIN:
                return "black"

    @staticmethod
    def _is_protected_mod(entry: NodeModEntry) -> bool:
        return entry.mod_type in {ModType.COREMOD, ModType.BUILTIN}

    def _resolve_mod_entry(self, *, model: ModWebPageModel, mod_name: str) -> NodeModEntry:
        for entry in model.mods.mods:
            if entry.name == mod_name:
                return entry
        raise ValueError(f"Unknown mod: {mod_name}")

    def _user_can_mutate_mod(
        self,
        *,
        user: ModWebUser,
        entry: NodeModEntry,
        action: NodeModMutationAction,
    ) -> bool:
        if self._is_builtin_mod(entry):
            return False
        required_level = required_mod_mutation_level(action, is_protected=self._is_protected_mod(entry))
        return self._user_has_level(user, required_level)

    def _available_mod_actions(
        self,
        *,
        user: ModWebUser,
        entry: NodeModEntry,
    ) -> tuple[NodeModMutationAction, ...]:
        ordered_actions: tuple[NodeModMutationAction, ...]
        if entry.placement is ModPlacement.CLIENT_ONLY:
            ordered_actions = (NodeModMutationAction.DELETE,)
        else:
            ordered_actions = (
                NodeModMutationAction.DISABLE if entry.enabled else NodeModMutationAction.ENABLE,
                NodeModMutationAction.DELETE,
            )
        return tuple(
            action for action in ordered_actions if self._user_can_mutate_mod(user=user, entry=entry, action=action)
        )

    async def _remote_app_mutation_async(
        self,
        node: ModWebNodeLink,
        app_name: str,
        action: NodeAppMutationAction,
        user: ModWebUser,
        friendly_name: str | None = None,
        title_font_preset: str | None = None,
        notes: str | None = None,
        lifecycle_notice_started: bool | None = None,
        lifecycle_notice_stopped: bool | None = None,
        lifecycle_notice_crashed: bool | None = None,
        relay_notice_player_session: bool | None = None,
        relay_notice_player_death: bool | None = None,
        relay_notice_progress: bool | None = None,
        relay_advancements_enabled: bool | None = None,
        disabled_activity_provider_ids: tuple[str, ...] | None = None,
        running_cpu_points: int | None = None,
        running_ram_points: int | None = None,
        startup_cpu_points: int | None = None,
        startup_ram_points: int | None = None,
        steam_update_enabled: bool | None = None,
        steam_update_selected_branch: str | None = None,
        update_branch_id: str | None = None,
        timeout_seconds: float = _REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
    ) -> NodeAppMutationResult:
        json_payload: dict[str, object] = {"action": action.value}
        if friendly_name is not None:
            json_payload["friendly_name"] = friendly_name
        if title_font_preset is not None:
            json_payload["title_font_preset"] = title_font_preset
        if notes is not None:
            json_payload["notes"] = notes
        if lifecycle_notice_started is not None:
            json_payload["lifecycle_notice_started"] = lifecycle_notice_started
        if lifecycle_notice_stopped is not None:
            json_payload["lifecycle_notice_stopped"] = lifecycle_notice_stopped
        if lifecycle_notice_crashed is not None:
            json_payload["lifecycle_notice_crashed"] = lifecycle_notice_crashed
        if relay_notice_player_session is not None:
            json_payload["relay_notice_player_session"] = relay_notice_player_session
        if relay_notice_player_death is not None:
            json_payload["relay_notice_player_death"] = relay_notice_player_death
        if relay_notice_progress is not None:
            json_payload["relay_notice_progress"] = relay_notice_progress
        if relay_advancements_enabled is not None:
            json_payload["relay_advancements_enabled"] = relay_advancements_enabled
        if disabled_activity_provider_ids is not None:
            json_payload["disabled_activity_provider_ids"] = list(disabled_activity_provider_ids)
        if action is NodeAppMutationAction.UPDATE_DETAILS:
            json_payload["running_cpu_points"] = running_cpu_points
            json_payload["running_ram_points"] = running_ram_points
            json_payload["startup_cpu_points"] = startup_cpu_points
            json_payload["startup_ram_points"] = startup_ram_points
            json_payload["steam_update_enabled"] = steam_update_enabled
            json_payload["steam_update_selected_branch"] = steam_update_selected_branch
        if update_branch_id is not None:
            json_payload["update_branch_id"] = update_branch_id
        payload = await self._remote_json_async(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/mutate",
            scopes=(required_app_mutation_scope(action),),
            user=user,
            method="POST",
            json_payload=json_payload,
            timeout=timeout_seconds,
        )
        return NodeAppMutationResult.from_mapping(payload)

    async def _remote_minecraft_recipe_mutation_async(
        self,
        node: ModWebNodeLink,
        app_name: str,
        action: NodeMinecraftRecipeMutationAction,
        mutation_index: int | None,
        mutation: MinecraftRecipeMutation | None,
        user: ModWebUser,
        timeout_seconds: float = _REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
    ) -> NodeMinecraftRecipeMutationResult:
        json_payload: dict[str, object] = {"action": action.value}
        if mutation_index is not None:
            json_payload["mutation_index"] = mutation_index
        if mutation is not None:
            json_payload["mutation"] = mutation.to_mapping()
        payload = await self._remote_json_async(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/minecraft/recipes/mutations",
            scopes=(NodeApiScope.APP_MANAGE,),
            user=user,
            method="POST",
            json_payload=json_payload,
            timeout=timeout_seconds,
        )
        return NodeMinecraftRecipeMutationResult.from_mapping(payload)

    async def _remote_node_capacity_async(
        self,
        node: ModWebNodeLink,
        user: ModWebUser,
    ) -> config.NodeCapacityProfile:
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/node-capacity",
            scopes=(NodeApiScope.NODE_MANAGE,),
            user=user,
        )
        return config.NodeCapacityProfile.model_validate(payload)

    async def _remote_node_disk_settings_async(
        self,
        node: ModWebNodeLink,
        user: ModWebUser,
    ) -> NodeDiskManagementState:
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/node-disk-settings",
            scopes=(NodeApiScope.NODE_MANAGE,),
            user=user,
        )
        return NodeDiskManagementState.from_mapping(payload)

    async def _remote_node_system_action_async(
        self,
        node: ModWebNodeLink,
        action: NodeSystemAction,
        auto_restart_running_apps: bool,
        user: ModWebUser,
    ) -> NodeSystemActionResult:
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/system/actions",
            scopes=(NodeApiScope.NODE_OPERATE,),
            user=user,
            method="POST",
            json_payload={
                "action": action.value,
                "auto_restart_running_apps": auto_restart_running_apps,
            },
        )
        return NodeSystemActionResult.from_mapping(payload)

    async def _remote_restart_schedules_async(
        self,
        node: ModWebNodeLink,
        user: ModWebUser,
    ) -> NodeRestartScheduleState:
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/system/restart-schedules",
            scopes=(NodeApiScope.NODE_OPERATE,),
            user=user,
        )
        return NodeRestartScheduleState.from_mapping(payload)

    async def _remote_update_restart_schedule_async(
        self,
        node: ModWebNodeLink,
        target: RestartTarget,
        interval_minutes: int | None,
        anchor_timestamp: int | None,
        user: ModWebUser,
    ) -> NodeRestartScheduleState:
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/system/restart-schedules",
            scopes=(NodeApiScope.NODE_OPERATE,),
            user=user,
            method="POST",
            json_payload={
                "target": target.value,
                "interval_minutes": interval_minutes,
                "anchor_timestamp": anchor_timestamp,
            },
        )
        return NodeRestartScheduleState.from_mapping(payload)

    async def _remote_skip_restart_schedule_async(
        self,
        node: ModWebNodeLink,
        target: RestartTarget,
        user: ModWebUser,
    ) -> NodeRestartScheduleState:
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path=f"/system/restart-schedules/{quote(target.value, safe='')}/skip",
            scopes=(NodeApiScope.NODE_OPERATE,),
            user=user,
            method="POST",
        )
        return NodeRestartScheduleState.from_mapping(payload)

    async def _remote_update_node_capacity_async(
        self,
        node: ModWebNodeLink,
        capacity: config.NodeCapacityProfile,
        user: ModWebUser,
    ) -> NodeCapacityMutationResult:
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/node-capacity",
            scopes=(NodeApiScope.NODE_MANAGE,),
            user=user,
            method="POST",
            json_payload=capacity.model_dump(mode="json"),
        )
        return NodeCapacityMutationResult.from_mapping(payload)

    async def _remote_update_node_disk_settings_async(
        self,
        node: ModWebNodeLink,
        preferences: config.PersistedDiskPreferences,
        user: ModWebUser,
    ) -> NodeDiskSettingsMutationResult:
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/node-disk-settings",
            scopes=(NodeApiScope.NODE_MANAGE,),
            user=user,
            method="POST",
            json_payload=preferences.model_dump(mode="json"),
        )
        return NodeDiskSettingsMutationResult.from_mapping(payload)

    async def _remote_node_font_sources_async(
        self,
        node: ModWebNodeLink,
        user: ModWebUser,
    ) -> config.NodeFontSourceSettings:
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/node-font-sources",
            scopes=(NodeApiScope.NODE_OPERATE,),
            user=user,
        )
        return config.NodeFontSourceSettings.model_validate(payload)

    async def _remote_update_node_font_sources_async(
        self,
        node: ModWebNodeLink,
        settings: config.NodeFontSourceSettings,
        user: ModWebUser,
    ) -> NodeFontSourceSettingsMutationResult:
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/node-font-sources",
            scopes=(NodeApiScope.NODE_OPERATE,),
            user=user,
            method="POST",
            json_payload=settings.model_dump(mode="json"),
        )
        return NodeFontSourceSettingsMutationResult.from_mapping(payload)

    async def _mutate_mod(
        self,
        *,
        model: ModWebPageModel,
        mod_name: str,
        action: NodeModMutationAction,
        user: ModWebUser,
    ) -> NodeModMutationResult:
        entry = self._resolve_mod_entry(model=model, mod_name=mod_name)
        if self._is_builtin_mod(entry):
            raise PermissionError("Built-in mods cannot be changed from mod web.")
        required_level = required_mod_mutation_level(action, is_protected=self._is_protected_mod(entry))
        if not self._user_has_level(user, required_level):
            raise PermissionError(f"{required_level.name.title()} access is required for this mod action.")
        node = self._remote_node_link(model.node_name)
        return await self._remote_mod_mutation_async(node, model.app_name, mod_name, action, user)

    async def _remote_mod_mutation_async(
        self,
        node: ModWebNodeLink,
        app_name: str,
        mod_name: str,
        action: NodeModMutationAction,
        user: ModWebUser,
    ) -> NodeModMutationResult:
        payload = await self._remote_json_async(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/mods/{quote(mod_name, safe='')}/mutate",
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="POST",
            json_payload={"action": action.value},
        )
        return NodeModMutationResult.from_mapping(payload)

    async def _update_mod_properties(
        self,
        *,
        model: ModWebPageModel,
        entry: NodeModEntry,
        mod_type: ModType,
        download_block_reason: ModDownloadBlockReason | None,
        metadata_overrides: ModMetadataOverrides,
        client_pack: ClientPackConfig | None = None,
        launcher_urls: LauncherProviderUrls,
        user: ModWebUser,
    ) -> NodeModMutationResult:
        if self._is_builtin_mod(entry):
            raise PermissionError("Built-in mod properties cannot be changed from mod web.")
        required_level = required_mod_mutation_level(NodeModMutationAction.UPDATE_PROPERTIES)
        if not self._user_has_level(user, required_level):
            raise PermissionError(f"{required_level.name.title()} access is required to edit mod properties.")
        payload = await self._remote_json_async(
            node=self._remote_node_link(model.node_name),
            app_name=model.app_name,
            path=f"/apps/{quote(model.app_name, safe='')}/mods/{quote(entry.name, safe='')}/properties",
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="PUT",
            json_payload={
                "mod_type": mod_type.value,
                "download_block_reason": (
                    download_block_reason.value if download_block_reason is not None else None
                ),
                "metadata_overrides": metadata_overrides.model_dump(mode="json"),
                "client_pack": (client_pack or entry.client_pack).model_dump(mode="json"),
                "launcher_urls": launcher_urls.model_dump(mode="json"),
            },
        )
        return NodeModMutationResult.from_mapping(payload)

    @staticmethod
    def _mod_action_label(action: NodeModMutationAction, entry: NodeModEntry) -> str:
        match action:
            case NodeModMutationAction.ENABLE:
                return "Enable"
            case NodeModMutationAction.DISABLE:
                return "Disable"
            case NodeModMutationAction.TOGGLE_COREMOD:
                return "Coremod"
            case NodeModMutationAction.TOGGLE_DOWNLOAD_BLOCK:
                return "Unblock" if not entry.downloadable else "Block"
            case NodeModMutationAction.UPDATE_PROPERTIES:
                return "Save Properties"
            case NodeModMutationAction.DELETE:
                return "Delete"
            case _:
                assert_never(action)

    @staticmethod
    def _mod_action_button_classes(action: NodeModMutationAction, entry: NodeModEntry) -> str:
        match action:
            case NodeModMutationAction.ENABLE:
                return "mod-list-button state-disabled"
            case NodeModMutationAction.DISABLE:
                return "mod-list-button state-enabled"
            case NodeModMutationAction.TOGGLE_COREMOD:
                return "mod-list-button state-core-on" if entry.coremod else "mod-list-button state-core-off"
            case NodeModMutationAction.TOGGLE_DOWNLOAD_BLOCK:
                return "mod-list-button state-blocked" if not entry.downloadable else "mod-list-button state-open"
            case NodeModMutationAction.UPDATE_PROPERTIES:
                return "mod-list-button"
            case NodeModMutationAction.DELETE:
                return "mod-list-button danger"
            case _:
                assert_never(action)

    @staticmethod
    def _is_builtin_mod(entry: NodeModEntry) -> bool:
        return entry.mod_type is ModType.BUILTIN

    @staticmethod
    def _selection_toggle_label(*, selected_count: int) -> str:
        return "Clear" if selected_count > 0 else "Select All"

    @staticmethod
    def _download_selection_label(*, selected_count: int, downloadable_count: int) -> str:
        if downloadable_count <= 0:
            return "Download 0/0"
        if selected_count <= 0 or selected_count >= downloadable_count:
            current = "All"
        else:
            current: str = str(selected_count)
        return f"Download {current}/{downloadable_count}"

    @staticmethod
    def _delete_selection_label(*, selected_count: int) -> str:
        if selected_count <= 0:
            return "Delete"
        return f"Delete {selected_count}"

    @staticmethod
    def _mod_result_count_label(*, visible_count: int, total_count: int) -> str:
        if visible_count < 0 or total_count < 0 or visible_count > total_count:
            raise ValueError("Mod result counts must satisfy 0 <= visible_count <= total_count.")
        mod_label: str = "mod" if total_count == 1 else "mods"
        if visible_count == total_count:
            return f"{total_count} {mod_label}"
        return f"{visible_count} of {total_count} {mod_label}"

    async def _mutate_app(
        self,
        *,
        model: ModWebBasePageModel,
        action: NodeAppMutationAction,
        user: ModWebUser,
        friendly_name: str | None = None,
        title_font_preset: str | None = None,
        notes: str | None = None,
        lifecycle_notice_started: bool | None = None,
        lifecycle_notice_stopped: bool | None = None,
        lifecycle_notice_crashed: bool | None = None,
        relay_notice_player_session: bool | None = None,
        relay_notice_player_death: bool | None = None,
        relay_notice_progress: bool | None = None,
        relay_advancements_enabled: bool | None = None,
        disabled_activity_provider_ids: tuple[str, ...] | None = None,
        running_cpu_points: int | None = None,
        running_ram_points: int | None = None,
        startup_cpu_points: int | None = None,
        startup_ram_points: int | None = None,
        steam_update_enabled: bool | None = None,
        steam_update_selected_branch: str | None = None,
        update_branch_id: str | None = None,
        timeout_seconds: float = _REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
    ) -> NodeAppMutationResult:
        required_level: Power_Level = required_app_mutation_level(action)
        if not self._user_has_level(user, required_level):
            raise PermissionError(f"{required_level.name.title()} access is required for this app action.")
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await self._remote_app_mutation_async(
            node=node,
            app_name=model.app_name,
            action=action,
            user=user,
            friendly_name=friendly_name,
            title_font_preset=title_font_preset,
            notes=notes,
            lifecycle_notice_started=lifecycle_notice_started,
            lifecycle_notice_stopped=lifecycle_notice_stopped,
            lifecycle_notice_crashed=lifecycle_notice_crashed,
            relay_notice_player_session=relay_notice_player_session,
            relay_notice_player_death=relay_notice_player_death,
            relay_notice_progress=relay_notice_progress,
            relay_advancements_enabled=relay_advancements_enabled,
            disabled_activity_provider_ids=disabled_activity_provider_ids,
            running_cpu_points=running_cpu_points,
            running_ram_points=running_ram_points,
            startup_cpu_points=startup_cpu_points,
            startup_ram_points=startup_ram_points,
            steam_update_enabled=steam_update_enabled,
            steam_update_selected_branch=steam_update_selected_branch,
            update_branch_id=update_branch_id,
            timeout_seconds=timeout_seconds,
        )

    async def _append_minecraft_recipe_mutation(
        self,
        *,
        model: ModWebBasePageModel,
        mutation: MinecraftRecipeMutation,
        user: ModWebUser,
        timeout_seconds: float = _REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
    ) -> NodeMinecraftRecipeMutationResult:
        self._require_user_level(user=user, required_level=Power_Level.sudo)
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await self._remote_minecraft_recipe_mutation_async(
            node=node,
            app_name=model.app_name,
            action=NodeMinecraftRecipeMutationAction.ADD,
            mutation_index=None,
            mutation=mutation,
            user=user,
            timeout_seconds=timeout_seconds,
        )

    async def _replace_minecraft_recipe_mutation(
        self,
        *,
        model: ModWebBasePageModel,
        mutation_index: int,
        mutation: MinecraftRecipeMutation,
        user: ModWebUser,
        timeout_seconds: float = _REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
    ) -> NodeMinecraftRecipeMutationResult:
        self._require_user_level(user=user, required_level=Power_Level.sudo)
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await self._remote_minecraft_recipe_mutation_async(
            node=node,
            app_name=model.app_name,
            action=NodeMinecraftRecipeMutationAction.REPLACE,
            mutation_index=mutation_index,
            mutation=mutation,
            user=user,
            timeout_seconds=timeout_seconds,
        )

    async def _delete_minecraft_recipe_mutation(
        self,
        *,
        model: ModWebBasePageModel,
        mutation_index: int,
        user: ModWebUser,
        timeout_seconds: float = _REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
    ) -> NodeMinecraftRecipeMutationResult:
        self._require_user_level(user=user, required_level=Power_Level.sudo)
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await self._remote_minecraft_recipe_mutation_async(
            node=node,
            app_name=model.app_name,
            action=NodeMinecraftRecipeMutationAction.DELETE,
            mutation_index=mutation_index,
            mutation=None,
            user=user,
            timeout_seconds=timeout_seconds,
        )

    async def _node_capacity(self, *, node_name: str, user: ModWebUser) -> config.NodeCapacityProfile:
        self._require_user_level(user=user, required_level=Power_Level.root)
        node = self._remote_node_link(node_name)
        return await self._remote_node_capacity_async(node, user)

    async def _node_font_sources(self, *, node_name: str, user: ModWebUser) -> config.NodeFontSourceSettings:
        self._require_user_level(user=user, required_level=Power_Level.sudo)
        node = self._remote_node_link(node_name)
        return await self._remote_node_font_sources_async(node, user)

    async def _node_disk_settings(self, *, node_name: str, user: ModWebUser) -> NodeDiskManagementState:
        self._require_user_level(user=user, required_level=Power_Level.root)
        node = self._remote_node_link(node_name)
        return await self._remote_node_disk_settings_async(node, user)

    async def _update_node_capacity(
        self,
        *,
        node_name: str,
        user: ModWebUser,
        capacity: config.NodeCapacityProfile,
    ) -> NodeCapacityMutationResult:
        self._require_user_level(user=user, required_level=Power_Level.root)
        node = self._remote_node_link(node_name)
        return await self._remote_update_node_capacity_async(node, capacity, user)

    async def _update_node_font_sources(
        self,
        *,
        node_name: str,
        user: ModWebUser,
        settings: config.NodeFontSourceSettings,
    ) -> NodeFontSourceSettingsMutationResult:
        self._require_user_level(user=user, required_level=Power_Level.sudo)
        node = self._remote_node_link(node_name)
        return await self._remote_update_node_font_sources_async(node, settings, user)

    async def _update_node_disk_settings(
        self,
        *,
        node_name: str,
        user: ModWebUser,
        preferences: config.PersistedDiskPreferences,
    ) -> NodeDiskSettingsMutationResult:
        self._require_user_level(user=user, required_level=Power_Level.root)
        node = self._remote_node_link(node_name)
        return await self._remote_update_node_disk_settings_async(node, preferences, user)

    @staticmethod
    def _app_start_stop_action(model: ModWebBasePageModel) -> NodeAppMutationAction | None:
        app_stats: NodeAppRuntimeSummary | None = model.app_stats
        if app_stats is not None and app_stats.transition_state is not NodeAppTransitionState.NONE:
            return None
        if app_stats is not None and app_stats.running:
            return NodeAppMutationAction.STOP
        if model.app_start_blocked:
            return None
        return NodeAppMutationAction.START

    @staticmethod
    def _app_start_stop_label(model: ModWebBasePageModel) -> str:
        app_stats: NodeAppRuntimeSummary | None = model.app_stats
        if app_stats is not None:
            if app_stats.transition_state is NodeAppTransitionState.STARTING:
                return "Starting"
            if app_stats.transition_state is NodeAppTransitionState.STOPPING:
                return "Stopping"
        if app_stats is not None and app_stats.running:
            return "Stop"
        if model.app_start_blocked:
            return "Blocked"
        return "Start"

    @staticmethod
    def _app_start_stop_button_classes(model: ModWebBasePageModel) -> str:
        app_stats: NodeAppRuntimeSummary | None = model.app_stats
        if app_stats is not None:
            if app_stats.transition_state is NodeAppTransitionState.STARTING:
                return "mod-list-button state-open"
            if app_stats.transition_state is NodeAppTransitionState.STOPPING:
                return "mod-list-button danger"
        if app_stats is not None and app_stats.running:
            return "mod-list-button danger"
        if model.app_start_blocked:
            return "mod-list-button state-blocked"
        return "mod-list-button state-enabled"

    @staticmethod
    def _app_start_stop_disabled(model: ModWebBasePageModel) -> bool:
        app_stats: NodeAppRuntimeSummary | None = model.app_stats
        if app_stats is not None and app_stats.transition_state is not NodeAppTransitionState.NONE:
            return True
        if app_stats is not None and app_stats.running:
            return False
        if model.app_start_blocked:
            return True
        return app_stats is not None and not app_stats.enabled

    @staticmethod
    def _app_kill_disabled(model: ModWebBasePageModel) -> bool:
        app_stats: NodeAppRuntimeSummary | None = model.app_stats
        if app_stats is not None and app_stats.running:
            return False
        if app_stats is not None and app_stats.transition_state is NodeAppTransitionState.STARTING:
            return False
        return True

    @staticmethod
    def _app_action_transition_state(action: NodeAppMutationAction) -> NodeAppTransitionState:
        if action is NodeAppMutationAction.START:
            return NodeAppTransitionState.STARTING
        if action in {NodeAppMutationAction.STOP, NodeAppMutationAction.KILL}:
            return NodeAppTransitionState.STOPPING
        return NodeAppTransitionState.NONE

    @classmethod
    def _start_stop_control_state(cls, model: ModWebBasePageModel) -> _ModWebStartStopControlState:
        return _ModWebStartStopControlState(
            label=cls._app_start_stop_label(model),
            button_classes=f"{cls._app_start_stop_button_classes(model)} mod-toolbar-button",
            disabled=cls._app_start_stop_disabled(model),
            action=cls._app_start_stop_action(model),
        )

    @staticmethod
    def _kill_control_state(model: ModWebBasePageModel) -> _ModWebKillControlState:
        return _ModWebKillControlState(
            label="Kill",
            disabled=ModWebActionsMixin._app_kill_disabled(model),
        )

    @classmethod
    def _app_action_pending_label(cls, action: NodeAppMutationAction) -> str | None:
        if action is NodeAppMutationAction.START:
            return "Starting..."
        if action is NodeAppMutationAction.STOP:
            return "Stopping..."
        if action is NodeAppMutationAction.KILL:
            return "Killing..."
        if action is NodeAppMutationAction.UPDATE:
            return "Updating..."
        if action is NodeAppMutationAction.VERIFY:
            return "Verifying..."
        return None

    @classmethod
    def _app_action_pending_message(cls, action: NodeAppMutationAction, app_friendly: str) -> str | None:
        if action is NodeAppMutationAction.START:
            return f"Start requested for {app_friendly}."
        if action is NodeAppMutationAction.STOP:
            return f"Stop requested for {app_friendly}."
        if action is NodeAppMutationAction.KILL:
            return f"Kill requested for {app_friendly}."
        if action is NodeAppMutationAction.UPDATE:
            return f"Update requested for {app_friendly}."
        if action is NodeAppMutationAction.VERIFY:
            return f"Verify requested for {app_friendly}."
        return None

    @staticmethod
    def _app_action_completion_message(*, pending_message: str | None, result_message: str) -> str | None:
        if result_message == pending_message:
            return None
        return result_message

    @staticmethod
    def _app_enable_disable_action(model: ModWebBasePageModel) -> NodeAppMutationAction:
        app_stats: NodeAppRuntimeSummary | None = model.app_stats
        if app_stats is not None and app_stats.enabled:
            return NodeAppMutationAction.DISABLE
        return NodeAppMutationAction.ENABLE

    @staticmethod
    def _app_enable_disable_label(model: ModWebBasePageModel) -> str:
        app_stats: NodeAppRuntimeSummary | None = model.app_stats
        if app_stats is not None and app_stats.enabled:
            return "Disable"
        return "Enable"

    @staticmethod
    def _app_enable_disable_button_classes(model: ModWebBasePageModel) -> str:
        app_stats: NodeAppRuntimeSummary | None = model.app_stats
        if app_stats is not None and app_stats.enabled:
            return "mod-list-button state-enabled"
        return "mod-list-button state-disabled"

    def _render_mod_info_dialog(
        self,
        *,
        ui: ModWebUi,
        entry: NodeModEntry,
        model: ModWebPageModel,
        user: ModWebUser,
    ) -> "Dialog":
        status_text = entry.placement.label
        downloadable_text = "Yes" if entry.downloadable else "No"
        version_text: str = entry.version or "Unknown"
        block_text: str = entry.download_block_label or entry.download_block_reason or "None"
        available_actions = self._available_mod_actions(user=user, entry=entry)
        can_edit_properties: bool = not self._is_builtin_mod(entry) and self._user_has_level(
            user,
            required_mod_mutation_level(NodeModMutationAction.UPDATE_PROPERTIES),
        )
        supports_client_pack: bool = mod_capabilities_for_scope(model.app_scope).supports_client_pack
        launcher_metadata_providers = mod_capabilities_for_scope(model.app_scope).launcher_metadata_providers
        launcher_url_inputs: dict[Provider, Input] = {}
        curseforge_project_id_input: Input | None = None
        curseforge_file_id_input: Input | None = None
        curseforge_metadata = entry.platforms.curseforge
        use_curseforge_reference_inputs = Provider.CURSEFORGE in launcher_metadata_providers and (
            not has_curseforge_api_key()
            or (curseforge_metadata is not None and curseforge_metadata.page_url is None)
        )
        launcher_metadata_description = (
            "Paste the exact provider file page, or enter the numeric CurseForge project and file IDs. "
            "Leave fields blank to bundle the local file."
            if use_curseforge_reference_inputs
            else "Paste the exact provider file page. Leave it blank to bundle the local file."
        )
        active_metadata_panel: Literal["overrides", "launcher"] | None = None

        def toggle_metadata_panel(panel: Literal["overrides", "launcher"]) -> None:
            nonlocal active_metadata_panel
            active_metadata_panel = None if active_metadata_panel == panel else panel
            overrides_section.set_visibility(active_metadata_panel == "overrides")
            launcher_metadata_section.set_visibility(active_metadata_panel == "launcher")
            if active_metadata_panel == "overrides":
                overrides_button.classes(add="mod-details-tab-active")
            else:
                overrides_button.classes(remove="mod-details-tab-active")
            if active_metadata_panel == "launcher":
                launcher_metadata_button.classes(add="mod-details-tab-active")
            else:
                launcher_metadata_button.classes(remove="mod-details-tab-active")

        async def save_properties() -> None:
            try:
                selected_mod_type = ModType(str(mod_type_select.value))
                selected_block_reason_text = str(download_block_reason_select.value or "").strip()
                selected_block_reason = (
                    None
                    if not selected_block_reason_text
                    else ModDownloadBlockReason(selected_block_reason_text)
                )
                metadata_overrides = ModMetadataOverrides(
                    friendly_name=str(friendly_name_override_input.value or ""),
                    version=str(version_override_input.value or ""),
                    origin=str(origin_override_input.value or ""),
                    added=(
                        None
                        if not str(added_override_input.value or "").strip()
                        else datetime.fromisoformat(str(added_override_input.value))
                    ),
                )
                client_pack = entry.client_pack
                if supports_client_pack:
                    selected_client_pack_policy = ClientPackPolicy(_value_as_text(client_pack_policy_select))
                    client_pack = ClientPackConfig(
                        included_in_client=bool(_value_as_object(client_pack_included_checkbox)),
                        policy=selected_client_pack_policy,
                        choice_group=(
                            _value_as_text(client_pack_choice_group_input).strip()
                            if selected_client_pack_policy is ClientPackPolicy.ALTERNATIVE
                            else None
                        ),
                        default_choice=(
                            entry.client_pack.default_choice
                            if selected_client_pack_policy is ClientPackPolicy.ALTERNATIVE
                            else False
                        ),
                        default_selected=(
                            bool(_value_as_object(client_pack_default_selected_checkbox))
                            if selected_client_pack_policy is ClientPackPolicy.OPTIONAL
                            else False
                        ),
                    )
                curseforge_reference: CurseForgeFileReference | None = None
                if use_curseforge_reference_inputs:
                    if curseforge_project_id_input is None or curseforge_file_id_input is None:
                        raise RuntimeError("CurseForge identifier inputs were not rendered.")
                    project_id = _value_as_text(curseforge_project_id_input).strip()
                    file_id = _value_as_text(curseforge_file_id_input).strip()
                    if project_id or file_id:
                        curseforge_reference = CurseForgeFileReference.model_validate(
                            {"project_id": project_id, "file_id": file_id}
                        )
                launcher_urls = LauncherProviderUrls(
                    modrinth=(
                        _value_as_text(launcher_url_inputs[Provider.MODRINTH]).strip()
                        if Provider.MODRINTH in launcher_url_inputs
                        else None
                    ),
                    curseforge=(
                        _value_as_text(launcher_url_inputs[Provider.CURSEFORGE]).strip()
                        if Provider.CURSEFORGE in launcher_url_inputs
                        else None
                    ),
                    curseforge_reference=curseforge_reference,
                )
                result = await self._update_mod_properties(
                    model=model,
                    entry=entry,
                    mod_type=selected_mod_type,
                    download_block_reason=selected_block_reason,
                    metadata_overrides=metadata_overrides,
                    client_pack=client_pack,
                    launcher_urls=launcher_urls,
                    user=user,
                )
            except Exception as xcp:
                log.warning(
                    "Mod properties update failed: node=%s app=%s mod=%s error=%s",
                    model.node_name,
                    model.app_name,
                    entry.name,
                    xcp,
                )
                ui.notify(f"Mod properties update failed: {xcp}", type="negative")
                return
            dialog.close()
            ui.notify(result.message, type="positive")
            ui.navigate.reload()

        async def run_mod_action(action: NodeModMutationAction) -> None:
            try:
                result: NodeModMutationResult = await self._mutate_mod(
                    model=model, mod_name=entry.name, action=action, user=user
                )
            except Exception as xcp:
                log.warning(
                    "Mod mutation failed: node=%s app=%s mod=%s action=%s error=%s",
                    model.node_name,
                    model.app_name,
                    entry.name,
                    action.value,
                    xcp,
                )
                ui.notify(f"Mod action failed: {xcp}", type="negative")
                return
            dialog.close()
            ui.notify(result.message, type="positive")
            ui.navigate.reload()

        async def confirm_delete() -> None:
            delete_confirm_dialog.close()
            await run_mod_action(NodeModMutationAction.DELETE)

        def _create_mod_action_handler(
            action: NodeModMutationAction,
        ) -> Callable[[object | None], Awaitable[None]]:
            async def _handle_mod_action(_: object | None = None) -> None:
                await run_mod_action(action)

            return _handle_mod_action

        with ui.dialog() as delete_confirm_dialog:
            with ui.card().classes("mod-card mod-dialog-card"):
                with ui.column().classes("w-full gap-4 p-5"):
                    ui.label("Delete mod?").classes("text-xl font-black mod-title-small")
                    ui.label(f"{entry.friendly} will be removed from the server.").classes("mod-subtitle text-sm")
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("Cancel", on_click=delete_confirm_dialog.close).classes("mod-list-button secondary")
                        ui.button("Delete", on_click=confirm_delete).classes("mod-list-button danger")

        with ui.dialog() as dialog:
            with ui.card().classes("mod-card mod-dialog-card mod-app-details-dialog-card"):
                with ui.column().classes("w-full gap-4 p-5"):
                    with ui.column().classes("gap-0"):
                        ui.label(entry.friendly).classes("text-xl font-black mod-title-small")
                        ui.label(entry.name).classes("mod-subtitle text-sm break-all")
                    with ui.grid(columns=2).classes("mod-detail-grid"):
                        self._render_mod_detail_item(ui=ui, label="Status", value=status_text)
                        self._render_mod_detail_item(
                            ui=ui,
                            label="Server loadable",
                            value="Yes" if entry.server_loadable else "No",
                        )
                        self._render_mod_detail_item(
                            ui=ui,
                            label="Client-pack eligible",
                            value="Yes" if entry.client_pack_eligible else "No",
                        )
                        self._render_mod_detail_item(ui=ui, label="Type", value=entry.mod_type.label)
                        self._render_mod_detail_item(ui=ui, label="Size", value=entry.size_text)
                        self._render_mod_detail_item(ui=ui, label="Downloadable", value=downloadable_text)
                        self._render_mod_detail_item(ui=ui, label="Coremod", value="Yes" if entry.coremod else "No")
                        self._render_mod_detail_item(ui=ui, label="Origin", value=entry.origin)
                        self._render_mod_detail_item(ui=ui, label="Version", value=version_text)
                        self._render_mod_detail_item(ui=ui, label="Added", value=entry.added)
                        self._render_mod_detail_item(ui=ui, label="Blocked", value=block_text)
                        self._render_mod_detail_item(
                            ui=ui,
                            label="Client pack",
                            value=entry.client_pack.policy.label,
                        )
                        if entry.client_pack.policy is ClientPackPolicy.ALTERNATIVE:
                            assert entry.client_pack.choice_group is not None
                            self._render_mod_detail_item(
                                ui=ui,
                                label="Choice group",
                                value=entry.client_pack.choice_group,
                            )
                            self._render_mod_detail_item(
                                ui=ui,
                                label="Default choice",
                                value="Yes" if entry.client_pack.default_choice else "No",
                            )
                    if can_edit_properties:
                        with ui.column().classes("w-full gap-3 mod-app-details-section"):
                            ui.label("Classification").classes("mod-stat-label")
                            with ui.row().classes("w-full gap-2 flex-wrap"):
                                mod_type_select = (
                                    ui.select(
                                        {
                                            mod_type.value: mod_type.label
                                            for mod_type in ModType
                                            if mod_type is not ModType.BUILTIN
                                        },
                                        value=entry.mod_type.value,
                                        label="Type",
                                    )
                                    .props("filled square dense hide-bottom-space color=accent options-dark")
                                    .classes("mod-app-details-field")
                                )
                                download_block_reason_select = (
                                    ui.select(
                                        {
                                            "": "None",
                                            **{
                                                reason.value: reason.label
                                                for reason in ModDownloadBlockReason
                                                if reason
                                                not in {
                                                    ModDownloadBlockReason.BUILTIN,
                                                    ModDownloadBlockReason.SERVER_ONLY,
                                                }
                                            },
                                        },
                                        value=entry.download_block_reason or "",
                                        label="Download block reason",
                                    )
                                    .props("filled square dense hide-bottom-space color=accent options-dark")
                                    .classes("mod-app-details-field")
                                )
                            if supports_client_pack:
                                with ui.column().classes("w-full gap-2"):
                                    ui.label("Client pack").classes("mod-stat-label")
                                    ui.label(
                                        "Choose whether this mod is included in client packs and, when included, "
                                        "whether it is required, optional, or one of several alternatives."
                                    ).classes("mod-subtitle text-xs")
                                    client_pack_included_checkbox = ui.checkbox(
                                        "Included in Client",
                                        value=entry.client_pack.included_in_client,
                                    ).props("dense color=accent keep-color").classes(
                                        "mod-client-pack-checkbox w-full"
                                    )
                                    client_pack_policy_select = (
                                        ui.select(
                                            {policy.value: policy.label for policy in ClientPackPolicy},
                                            value=entry.client_pack.policy.value,
                                            label="Policy",
                                        )
                                        .props("filled square dense hide-bottom-space color=accent options-dark")
                                        .classes("mod-app-details-field mod-client-pack-select")
                                    )
                                    with ui.column().classes("w-full gap-1") as optional_client_pack_controls:
                                        client_pack_default_selected_checkbox = ui.checkbox(
                                            "Included by default",
                                            value=entry.client_pack.default_selected,
                                        ).props("dense color=accent keep-color").classes(
                                            "mod-client-pack-checkbox w-full"
                                        )
                                    with ui.column().classes("w-full gap-2") as alternative_client_pack_controls:
                                        client_pack_choice_group_input = (
                                            ui.input(
                                                "Choice group",
                                                value=entry.client_pack.choice_group or "",
                                                placeholder="e.g. minimap",
                                            )
                                            .props("filled square dense clearable hide-bottom-space color=accent")
                                            .classes("mod-app-details-field")
                                        )
                                    def refresh_client_pack_policy_controls() -> None:
                                        selected_policy = ClientPackPolicy(_value_as_text(client_pack_policy_select))
                                        optional_client_pack_controls.set_visibility(
                                            selected_policy is ClientPackPolicy.OPTIONAL
                                        )
                                        alternative_client_pack_controls.set_visibility(
                                            selected_policy is ClientPackPolicy.ALTERNATIVE
                                        )

                                    client_pack_policy_select.on(
                                        "update:model-value",
                                        lambda _: refresh_client_pack_policy_controls(),
                                    )
                                    refresh_client_pack_policy_controls()
                            with ui.row().classes("w-full gap-2 mod-details-tab-row"):
                                overrides_button = ui.button(
                                    "Overrides",
                                    on_click=lambda: toggle_metadata_panel("overrides"),
                                ).classes("mod-list-button secondary mod-details-tab-button")
                                launcher_metadata_button = ui.button(
                                    "Launcher Metadata",
                                    on_click=lambda: toggle_metadata_panel("launcher"),
                                ).classes("mod-list-button secondary mod-details-tab-button")
                                launcher_metadata_button.set_visibility(bool(launcher_metadata_providers))
                            with ui.column().classes("w-full gap-2") as overrides_section:
                                ui.label("Overrides").classes("mod-stat-label")
                                ui.label(
                                    "Blank values continue using metadata detected from the mod file."
                                ).classes("mod-subtitle text-xs")
                                friendly_name_override_input = (
                                    ui.input(
                                        "Display name",
                                        value=entry.metadata_overrides.friendly_name or "",
                                        placeholder=entry.friendly,
                                    )
                                    .props("filled square dense clearable hide-bottom-space color=accent maxlength=80")
                                    .classes("mod-app-details-field mod-mod-override-field")
                                )
                                version_override_input = (
                                    ui.input(
                                        "Version",
                                        value=entry.metadata_overrides.version or "",
                                        placeholder=entry.version or "Unknown",
                                    )
                                    .props("filled square dense clearable hide-bottom-space color=accent")
                                    .classes("mod-app-details-field mod-mod-override-field")
                                )
                                origin_override_input = (
                                    ui.input(
                                        "Origin",
                                        value=entry.metadata_overrides.origin or "",
                                        placeholder=entry.origin,
                                    )
                                    .props("filled square dense clearable hide-bottom-space color=accent")
                                    .classes("mod-app-details-field mod-mod-override-field")
                                )
                                added_override_input = (
                                    ui.input(
                                        "Added",
                                        value=(
                                            ""
                                            if entry.metadata_overrides.added is None
                                            else entry.metadata_overrides.added.isoformat(timespec="minutes")
                                        ),
                                        placeholder=entry.added,
                                    )
                                    .props(
                                        "filled square dense clearable hide-bottom-space color=accent "
                                        "type=datetime-local"
                                    )
                                    .classes(
                                        "mod-app-details-field mod-mod-override-field mod-mod-override-datetime"
                                    )
                                )
                            overrides_section.set_visibility(False)
                            with ui.column().classes("w-full gap-2") as launcher_metadata_section:
                                ui.label("Launcher Metadata").classes("mod-stat-label")
                                ui.label(launcher_metadata_description).classes("mod-subtitle text-xs")
                                with ui.column().classes("w-full gap-2"):
                                    for provider in launcher_metadata_providers:
                                        if provider is Provider.CURSEFORGE and use_curseforge_reference_inputs:
                                            curseforge_project_id_input = (
                                                ui.input(
                                                    "CurseForge project ID",
                                                    value=(
                                                        ""
                                                        if curseforge_metadata is None
                                                        else str(curseforge_metadata.project_id)
                                                    ),
                                                )
                                                .props(
                                                    "filled square dense clearable hide-bottom-space color=accent "
                                                    "type=number min=1 step=1"
                                                )
                                                .classes(
                                                    "w-full mod-app-details-field mod-mod-launcher-field"
                                                )
                                            )
                                            curseforge_file_id_input = (
                                                ui.input(
                                                    "CurseForge file ID",
                                                    value=(
                                                        ""
                                                        if curseforge_metadata is None
                                                        else str(curseforge_metadata.file_id)
                                                    ),
                                                )
                                                .props(
                                                    "filled square dense clearable hide-bottom-space color=accent "
                                                    "type=number min=1 step=1"
                                                )
                                                .classes(
                                                    "w-full mod-app-details-field mod-mod-launcher-field"
                                                )
                                            )
                                            continue
                                        launcher_url_inputs[provider] = (
                                            ui.input(
                                                f"{launcher_provider_label(provider)} file page",
                                                value=entry.platforms.page_url_for(provider) or "",
                                                placeholder="https://…",
                                            )
                                            .props("filled square dense clearable hide-bottom-space color=accent")
                                            .classes("w-full mod-app-details-field mod-mod-launcher-field")
                                        )
                            launcher_metadata_section.set_visibility(False)
                    if available_actions:
                        with ui.column().classes("gap-2"):
                            ui.label("Privileged Actions").classes("mod-stat-label")
                            with ui.row().classes("w-full gap-2 flex-wrap"):
                                for action in available_actions:
                                    on_click = (
                                        delete_confirm_dialog.open
                                        if action is NodeModMutationAction.DELETE
                                        else _create_mod_action_handler(action)
                                    )
                                    ui.button(
                                        self._mod_action_label(action, entry),
                                        on_click=on_click,
                                    ).classes(self._mod_action_button_classes(action, entry))
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("Close", on_click=dialog.close).classes("mod-list-button secondary")
                        if can_edit_properties:
                            ui.button("Save", on_click=save_properties).classes("mod-list-button")
        return dialog

    @staticmethod
    def _render_mod_detail_item(*, ui: ModWebUi, label: str, value: str) -> None:
        with ui.column().classes("mod-detail-item gap-1"):
            ui.label(label).classes("mod-stat-label")
            ui.label(value).classes("mod-stat-value break-words")

    def _render_mod_download_row(
        self,
        *,
        ui: ModWebUi,
        entry: NodeModEntry,
        download_url: str | None,
        on_change: Callable[["ValueChangeEventArguments"], None],
        can_select: bool,
        app_friendly: str,
        model: ModWebPageModel,
        user: ModWebUser,
    ) -> Checkbox | None:
        row_classes = ["mod-row", "w-full"]
        if not entry.downloadable:
            row_classes.append("blocked")
        elif entry.placement is ModPlacement.CLIENT_ONLY:
            row_classes.append("mod-row-client-only")
        elif entry.placement is ModPlacement.SERVER_DISABLED:
            row_classes.append("mod-row-disabled")
        dialog = self._render_mod_info_dialog(ui=ui, entry=entry, model=model, user=user)
        row = ui.row().classes(" ".join((*row_classes, "mod-row-clickable")))
        row.on("click", lambda _: dialog.open())
        with row:
            if can_select:
                checkbox = ui.checkbox(value=False, on_change=on_change).props("dense")
                checkbox.on("click", js_handler="(event) => event.stopPropagation()")
            else:
                checkbox = ui.checkbox(value=False).props("dense")
                checkbox.disable()
                checkbox.on("click", js_handler="(event) => event.stopPropagation()")
            with ui.column().classes("mod-row-main gap-0"):
                ui.label(entry.friendly).classes("mod-row-title")
                ui.label(entry.name).classes("mod-row-file")
            with ui.row().classes("mod-row-meta"):
                ui.label(entry.size_text).classes("mod-pill size")
                if entry.placement is ModPlacement.CLIENT_ONLY:
                    ui.label(entry.placement.label).classes("mod-pill")
                if entry.client_pack.policy is not ClientPackPolicy.REQUIRED:
                    ui.label(entry.client_pack.policy.label).classes("mod-pill")
                show_download_block_badge: bool = not entry.downloadable and not (
                    entry.mod_type is ModType.SERVER
                    and entry.download_block_reason == ModDownloadBlockReason.SERVER_ONLY.value
                )
                if show_download_block_badge:
                    ui.label(entry.download_block_label or "Not downloadable").classes("mod-pill blocked")
            if download_url is None:
                ui.label("Blocked").classes("mod-row-download blocked")
            else:

                async def download_single() -> None:
                    await self._start_download(
                        ui=ui,
                        user=user,
                        model=model,
                        url=download_url,
                        message=self._download_feedback_message(
                            kind=ModDownloadKind.SINGLE,
                            app_friendly=app_friendly,
                            mod_friendly=entry.friendly,
                        ),
                        filenames=(entry.name,),
                    )

                ui.button("Download", on_click=download_single).props("flat dense no-caps").classes(
                    "mod-row-download"
                ).on("click", js_handler="(event) => event.stopPropagation()")
            with ui.column().classes("mod-setting-badge-rail mod-mod-type-badge-rail"):
                self._badge(
                    ui=ui,
                    text=entry.mod_type.label,
                    tone=self._mod_type_badge_tone(entry.mod_type),
                    extra_classes="mod-setting-badge mod-mod-type-badge",
                )
        return checkbox if can_select else None

    async def _start_download(
        self,
        *,
        ui: ModWebUi,
        user: ModWebUser,
        model: ModWebBasePageModel,
        url: str,
        message: str,
        filenames: tuple[str, ...],
    ) -> None:
        try:
            self._backend.start_download_transfers(
                user_id=user.discord_id,
                filenames=filenames,
                detail_text=message,
                node_color_hex=self._node_role_color_hex(node_name=model.node_name),
                app_color_hex=model.app_color_hex,
            )
        except RuntimeError as xcp:
            ui.notify(str(xcp), type="warning")
            return
        ui.notify(message, type="info")
        await asyncio.sleep(_DOWNLOAD_FEEDBACK_DELAY_SECONDS)
        download = getattr(ui, "download", None)
        if callable(download):
            download(url)
            return
        ui.navigate.to(url)

    @staticmethod
    def _download_feedback_message(
        *,
        kind: ModDownloadKind,
        app_friendly: str,
        mod_friendly: str | None = None,
        selected_count: int | None = None,
    ) -> str:
        match kind:
            case ModDownloadKind.ENABLED:
                return f"Preparing enabled mod download for {app_friendly}."
            case ModDownloadKind.ALL:
                return f"Preparing full mod download for {app_friendly}."
            case ModDownloadKind.CLIENT_PACK:
                return f"Preparing client pack for {app_friendly}."
            case ModDownloadKind.SELECTED:
                if selected_count is None or selected_count < 1:
                    raise ValueError("Selected downloads require a positive selected_count.")
                mod_label = "mod" if selected_count == 1 else "mods"
                return f"Preparing download for {selected_count} selected {mod_label} from {app_friendly}."
            case ModDownloadKind.SINGLE:
                if mod_friendly is None or not mod_friendly.strip():
                    raise ValueError("Single downloads require a mod_friendly value.")
                return f"Preparing download for {mod_friendly} from {app_friendly}."
            case _:
                assert_never(kind)
