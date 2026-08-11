from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from modmux.models import Provider

from apps._config import (
    CurseForgeFileReference,
    KnownModPageProvider,
    LauncherMetadataMatchReason,
    LauncherProviderUrls,
    ModPageMatchConfidence,
    known_mod_page_provider_for_url,
    launcher_provider_label,
    mod_capabilities_for_scope,
    mod_pages_in_display_order,
    normalise_mod_page_url,
)
from apps._launcher_metadata import has_curseforge_api_key, launcher_project_page_url
from apps.minecraft import MinecraftRecipeMutation

from .constants import (
    _BULK_METADATA_REQUEST_TIMEOUT_SECONDS,
    _DOWNLOAD_FEEDBACK_DELAY_SECONDS,
    _MOD_UPDATE_CHECK_CACHE_MAX_ENTRIES,
    _MOD_UPDATE_CHECK_CACHE_TTL_SECONDS,
    _REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
    log,
)
from .nicegui_protocols import ModWebUi, _value_as_object, _value_as_text
from .runtime_imports import (
    Awaitable,
    BadgeTone,
    BulkLauncherMetadataDiscovery,
    Button,
    Callable,
    Checkbox,
    ClientPackConfig,
    ClientPackPolicy,
    Column,
    Input,
    Label,
    LauncherMetadataDiscovery,
    LauncherMetadataResolution,
    Literal,
    ModDownloadBlockReason,
    ModMetadataOverrides,
    ModPageDiscovery,
    ModPageLink,
    ModPlacement,
    ModType,
    ModWebUser,
    NodeApiScope,
    NodeAppMutationAction,
    NodeAppMutationResult,
    NodeAppRuntimeSummary,
    NodeAppTransitionState,
    NodeBulkLauncherMetadataApplyResult,
    NodeCapacityMutationResult,
    NodeDiskManagementState,
    NodeDiskSettingsMutationResult,
    NodeDiscordSettingsMutationResult,
    NodeFontSourceSettingsMutationResult,
    NodeMinecraftRecipeMutationAction,
    NodeMinecraftRecipeMutationResult,
    NodeModEntry,
    NodeModMutationAction,
    NodeModMutationResult,
    NodeModPortalVersionEntry,
    NodeModPortalVersionList,
    NodeModUpdateCheckResult,
    NodeModUpdateDependencyAction,
    NodeModUpdateStatus,
    NodeModUploadBatchResult,
    NodeRestartScheduleState,
    NodeRestartState,
    NodeSystemAction,
    NodeSystemActionResult,
    NodeSystemCapabilities,
    Power_Level,
    RestartTarget,
    Select,
    Textarea,
    assert_never,
    asyncio,
    config,
    datetime,
    quote,
    required_app_mutation_level,
    required_app_mutation_scope,
    required_mod_mutation_level,
    urlencode,
)
from .service_base import ModWebServiceSupport
from .types import (
    ModDownloadKind,
    ModWebBasePageModel,
    ModWebNodeLink,
    ModWebPageModel,
    _ModWebKillControlState,
    _ModWebModUpdateBatchResult,
    _ModWebModUpdateCacheEntry,
    _ModWebModUpdateCacheKey,
    _ModWebStartStopControlState,
)

if TYPE_CHECKING:
    from nicegui.elements.dialog import Dialog
    from nicegui.events import ValueChangeEventArguments


@dataclass(slots=True)
class _ModPageEditorRow:
    container: Column
    name_input: Input
    url_input: Input
    automatic_name: str | None = None


def _launcher_provider_selection_payload(
    providers: tuple[Provider, ...] | None,
) -> dict[str, list[str]]:
    if providers is None:
        return {}
    return {"providers": [provider.value for provider in providers]}


class ModWebActionsMixin(ModWebServiceSupport):
    @staticmethod
    def _mod_update_cache_key(
        *,
        model: ModWebPageModel,
        entry: NodeModEntry,
    ) -> _ModWebModUpdateCacheKey:
        return _ModWebModUpdateCacheKey(
            node_name=model.node_name.casefold(),
            app_name=model.app_name.casefold(),
            mod_name=entry.name,
            installed_version=entry.version,
        )

    def _cached_mod_update_names(
        self,
        *,
        model: ModWebPageModel,
        entries: tuple[NodeModEntry, ...],
    ) -> frozenset[str]:
        now = time.monotonic()
        with self._mod_update_check_cache_lock:
            return frozenset(
                entry.name
                for entry in entries
                if (
                    cached := self._cached_mod_update_result_locked(
                        cache_key=self._mod_update_cache_key(model=model, entry=entry),
                        now=now,
                    )
                ) is not None
                and cached.status is NodeModUpdateStatus.UPDATE_AVAILABLE
            )

    def _cached_mod_update_result(
        self,
        *,
        model: ModWebPageModel,
        entry: NodeModEntry,
    ) -> NodeModUpdateCheckResult | None:
        cache_key = self._mod_update_cache_key(model=model, entry=entry)
        now = time.monotonic()
        with self._mod_update_check_cache_lock:
            return self._cached_mod_update_result_locked(cache_key=cache_key, now=now)

    def _cached_mod_update_result_locked(
        self,
        *,
        cache_key: _ModWebModUpdateCacheKey,
        now: float,
    ) -> NodeModUpdateCheckResult | None:
        cached = self._mod_update_check_cache.get(cache_key)
        if cached is None:
            return None
        if cached.expires_at_seconds <= now:
            del self._mod_update_check_cache[cache_key]
            return None
        return cached.result

    @staticmethod
    def _actionable_mod_update_result(result: NodeModUpdateCheckResult | None) -> NodeModUpdateCheckResult | None:
        if result is None or result.status is not NodeModUpdateStatus.UPDATE_AVAILABLE:
            return None
        if any(dependency.action is NodeModUpdateDependencyAction.BLOCKED for dependency in result.dependencies):
            return None
        return result

    def _cache_mod_update_result(
        self,
        *,
        model: ModWebPageModel,
        entry: NodeModEntry,
        result: NodeModUpdateCheckResult,
    ) -> None:
        cache_key = self._mod_update_cache_key(model=model, entry=entry)
        now = time.monotonic()
        cache_entry = _ModWebModUpdateCacheEntry(
            result=result,
            expires_at_seconds=now + _MOD_UPDATE_CHECK_CACHE_TTL_SECONDS,
        )
        with self._mod_update_check_cache_lock:
            expired_keys = tuple(
                key
                for key, cached in self._mod_update_check_cache.items()
                if cached.expires_at_seconds <= now
            )
            for expired_key in expired_keys:
                del self._mod_update_check_cache[expired_key]
            if cache_key not in self._mod_update_check_cache and len(self._mod_update_check_cache) >= _MOD_UPDATE_CHECK_CACHE_MAX_ENTRIES:
                oldest_key = min(
                    self._mod_update_check_cache,
                    key=lambda key: self._mod_update_check_cache[key].expires_at_seconds,
                )
                del self._mod_update_check_cache[oldest_key]
            self._mod_update_check_cache[cache_key] = cache_entry

    def _invalidate_mod_update_cache(self, *, model: ModWebPageModel, mod_name: str | None = None) -> None:
        node_name = model.node_name.casefold()
        app_name = model.app_name.casefold()
        with self._mod_update_check_cache_lock:
            cache_keys = tuple(
                key
                for key in self._mod_update_check_cache
                if key.node_name == node_name
                and key.app_name == app_name
                and (mod_name is None or key.mod_name == mod_name)
            )
            for cache_key in cache_keys:
                del self._mod_update_check_cache[cache_key]

    @staticmethod
    async def _run_with_loading_button(
        *,
        button: Button,
        action: Callable[[], Awaitable[None]],
    ) -> None:
        button.props("loading")
        try:
            await action()
        finally:
            button.props(remove="loading")

    @staticmethod
    def _automatic_mod_pages(discovery: ModPageDiscovery) -> tuple[ModPageLink, ...] | None:
        selected_pages: list[ModPageLink] = []
        for provider_result in discovery.providers:
            if not provider_result.candidates:
                continue
            confident_candidates = tuple(
                candidate
                for candidate in provider_result.candidates
                if candidate.confidence is not ModPageMatchConfidence.POSSIBLE
            )
            if len(confident_candidates) != 1:
                return None
            selected_pages.append(confident_candidates[0].page)
        return tuple(selected_pages)

    @staticmethod
    def _automatic_launcher_urls(
        discovery: LauncherMetadataDiscovery,
    ) -> dict[Provider, str] | None:
        selected_urls: dict[Provider, str] = {}
        for provider_result in discovery.providers:
            if not provider_result.candidates:
                continue
            if len(provider_result.candidates) != 1:
                return None
            candidate = provider_result.candidates[0]
            if candidate.match_reasons == (LauncherMetadataMatchReason.FILENAME,):
                return None
            selected_urls[provider_result.provider] = candidate.file_page_url
        return selected_urls

    @staticmethod
    def _launcher_providers_missing_mod_pages(
        *,
        providers: tuple[Provider, ...],
        launcher_urls: LauncherProviderUrls,
        mod_pages: tuple[ModPageLink, ...],
    ) -> frozenset[Provider]:
        known_provider_by_launcher = {
            Provider.MODRINTH: KnownModPageProvider.MODRINTH,
            Provider.CURSEFORGE: KnownModPageProvider.CURSEFORGE,
        }
        existing_page_providers = {
            page_provider
            for page in mod_pages
            if (page_provider := known_mod_page_provider_for_url(page.url)) is not None
        }
        return frozenset(
            provider
            for provider in providers
            if not launcher_urls.has_provider(provider)
            and known_provider_by_launcher[provider] not in existing_page_providers
        )

    @staticmethod
    def _client_pack_default_selected_after_policy_change(
        *,
        previous_policy: ClientPackPolicy,
        selected_policy: ClientPackPolicy,
        current_value: bool,
    ) -> bool:
        if (
            selected_policy is ClientPackPolicy.OPTIONAL
            and previous_policy is not ClientPackPolicy.OPTIONAL
        ):
            return True
        return current_value

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
    def _mod_download_summary(entry: NodeModEntry) -> str:
        if entry.downloadable:
            return "Available"
        reason = entry.download_block_label or entry.download_block_reason
        return "Blocked" if reason is None else f"Blocked — {reason}"

    @staticmethod
    def _mod_client_pack_summary(entry: NodeModEntry) -> str:
        if entry.mod_type is ModType.BUILTIN:
            return "Excluded — Built-in"
        if entry.placement is ModPlacement.SERVER_DISABLED:
            return "Excluded — Server disabled"
        if not entry.downloadable:
            return "Excluded — File download blocked"
        if not entry.client_pack.included_in_client:
            return "Not included"
        if not entry.client_pack_eligible:
            return "Excluded — Ineligible"

        match entry.client_pack.policy:
            case ClientPackPolicy.REQUIRED:
                return "Required"
            case ClientPackPolicy.OPTIONAL:
                default_state = "included" if entry.client_pack.default_selected else "not included"
                return f"Optional — {default_state} by default"
            case ClientPackPolicy.ALTERNATIVE:
                assert entry.client_pack.choice_group is not None
                suffix = " (default)" if entry.client_pack.default_choice else ""
                return f"Alternative — {entry.client_pack.choice_group}{suffix}"
            case _:
                assert_never(entry.client_pack.policy)

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
        factorio_chat_relay_use_shout: bool | None = None,
        rcon_requires_online_players: bool | None = None,
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
        if factorio_chat_relay_use_shout is not None:
            json_payload["factorio_chat_relay_use_shout"] = factorio_chat_relay_use_shout
        if rcon_requires_online_players is not None:
            json_payload["rcon_requires_online_players"] = rcon_requires_online_players
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

    async def _remote_discord_settings_async(
        self,
        node: ModWebNodeLink,
        user: ModWebUser,
    ) -> config.DiscordSettings:
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/discord-settings",
            scopes=(NodeApiScope.NODE_OPERATE,),
            user=user,
        )
        return config.DiscordSettings.model_validate(payload)

    async def _remote_node_system_action_async(
        self,
        node: ModWebNodeLink,
        action: NodeSystemAction,
        auto_restart_running_apps: bool,
        silent: bool,
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
                "silent": silent,
            },
        )
        return NodeSystemActionResult.from_mapping(payload)

    async def _remote_node_system_capabilities_async(
        self,
        node: ModWebNodeLink,
        user: ModWebUser,
    ) -> NodeSystemCapabilities:
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/system/capabilities",
            scopes=(NodeApiScope.NODE_OPERATE,),
            user=user,
        )
        return NodeSystemCapabilities.from_mapping(payload)

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

    async def _remote_restart_state_async(
        self,
        node: ModWebNodeLink,
        user: ModWebUser,
    ) -> NodeRestartState:
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/system/restart-state",
            scopes=(NodeApiScope.NODE_OPERATE,),
            user=user,
        )
        return NodeRestartState.from_mapping(payload)

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

    async def _remote_update_discord_settings_async(
        self,
        node: ModWebNodeLink,
        settings: config.DiscordSettings,
        user: ModWebUser,
    ) -> NodeDiscordSettingsMutationResult:
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/discord-settings",
            scopes=(NodeApiScope.NODE_OPERATE,),
            user=user,
            method="POST",
            json_payload=settings.model_dump(mode="json"),
        )
        return NodeDiscordSettingsMutationResult.from_mapping(payload)

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
        result = await self._remote_mod_mutation_async(node, model.app_name, mod_name, action, user)
        self._invalidate_mod_update_cache(model=model, mod_name=mod_name)
        return result

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

    async def _check_mod_update(
        self,
        *,
        model: ModWebPageModel,
        entry: NodeModEntry,
        user: ModWebUser,
        version: str | None = None,
    ) -> NodeModUpdateCheckResult:
        if model.app_scope != config.AppScopes.factorio.value:
            raise ValueError(f"{model.app_friendly} does not support mod update checks yet.")
        if not self._user_has_level(user, Power_Level.user):
            raise PermissionError("User access is required to check mod updates.")
        payload = await self._remote_json_async(
            node=self._remote_node_link(model.node_name),
            app_name=model.app_name,
            path=(
                f"/apps/{quote(model.app_name, safe='')}/mods/{quote(entry.name, safe='')}/check-update"
                f"{'?' + urlencode({'version': version}) if version is not None else ''}"
            ),
            scopes=(NodeApiScope.MODS_READ,),
            user=user,
        )
        result = NodeModUpdateCheckResult.from_mapping(payload)
        if version is None:
            self._cache_mod_update_result(model=model, entry=entry, result=result)
        return result

    async def _check_all_mod_updates(
        self,
        *,
        model: ModWebPageModel,
        entries: tuple[NodeModEntry, ...],
        user: ModWebUser,
        on_checking: Callable[[NodeModEntry], None] | None = None,
    ) -> _ModWebModUpdateBatchResult:
        update_mod_names: set[str] = set()
        failed_mod_names: list[str] = []
        for entry in entries:
            if on_checking is not None:
                on_checking(entry)
            try:
                result = await self._check_mod_update(model=model, entry=entry, user=user)
            except Exception as xcp:
                log.warning(
                    "Bulk mod update check failed: node=%s app=%s mod=%s error=%s",
                    model.node_name,
                    model.app_name,
                    entry.name,
                    xcp,
                )
                failed_mod_names.append(entry.name)
                continue
            if result.status is NodeModUpdateStatus.UPDATE_AVAILABLE:
                update_mod_names.add(entry.name)
        return _ModWebModUpdateBatchResult(
            checked_mod_count=len(entries),
            update_mod_names=frozenset(update_mod_names),
            failed_mod_names=tuple(failed_mod_names),
        )

    async def _mod_versions(
        self,
        *,
        model: ModWebPageModel,
        entry: NodeModEntry,
        user: ModWebUser,
    ) -> NodeModPortalVersionList:
        if model.app_scope != config.AppScopes.factorio.value:
            raise ValueError(f"{model.app_friendly} does not support mod version discovery yet.")
        if not self._user_has_level(user, Power_Level.user):
            raise PermissionError("User access is required to inspect mod versions.")
        payload = await self._remote_json_async(
            node=self._remote_node_link(model.node_name),
            app_name=model.app_name,
            path=f"/apps/{quote(model.app_name, safe='')}/mods/{quote(entry.name, safe='')}/versions",
            scopes=(NodeApiScope.MODS_READ,),
            user=user,
        )
        return NodeModPortalVersionList.from_mapping(payload)

    async def _update_mod(
        self,
        *,
        model: ModWebPageModel,
        entry: NodeModEntry,
        user: ModWebUser,
        version: str | None = None,
    ) -> NodeModUploadBatchResult:
        if model.app_scope != config.AppScopes.factorio.value:
            raise ValueError(f"{model.app_friendly} does not support mod updates yet.")
        if entry.placement is not ModPlacement.SERVER_ENABLED:
            raise ValueError(f"Only enabled mods can be updated: {entry.friendly}.")
        if not self._user_has_level(user, Power_Level.user):
            raise PermissionError("User access is required to update mods.")
        payload = await self._remote_json_async(
            node=self._remote_node_link(model.node_name),
            app_name=model.app_name,
            path=f"/apps/{quote(model.app_name, safe='')}/mods/{quote(entry.name, safe='')}/update",
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="POST",
            json_payload={"version": version},
        )
        result = NodeModUploadBatchResult.from_mapping(payload)
        self._invalidate_mod_update_cache(model=model, mod_name=entry.name)
        return result

    async def _update_mod_properties(
        self,
        *,
        model: ModWebPageModel,
        entry: NodeModEntry,
        mod_type: ModType,
        download_block_reason: ModDownloadBlockReason | None,
        metadata_overrides: ModMetadataOverrides,
        mod_pages: tuple[ModPageLink, ...] = (),
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
                "mod_pages": [page.model_dump(mode="json") for page in mod_pages],
                "client_pack": (client_pack or entry.client_pack).model_dump(mode="json"),
                "launcher_urls": launcher_urls.model_dump(mode="json"),
            },
        )
        result = NodeModMutationResult.from_mapping(payload)
        self._invalidate_mod_update_cache(model=model, mod_name=entry.name)
        return result

    async def _update_mod_notes(
        self,
        *,
        model: ModWebPageModel,
        entry: NodeModEntry,
        notes: str | None,
        user: ModWebUser,
    ) -> NodeModMutationResult:
        if not self._user_has_level(user, Power_Level.admin):
            raise PermissionError("Admin access is required to edit mod notes.")
        payload = await self._remote_json_async(
            node=self._remote_node_link(model.node_name),
            app_name=model.app_name,
            path=f"/apps/{quote(model.app_name, safe='')}/mods/{quote(entry.name, safe='')}/notes",
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="PUT",
            json_payload={"notes": notes},
        )
        return NodeModMutationResult.from_mapping(payload)

    async def _fetch_mod_launcher_metadata(
        self,
        *,
        model: ModWebPageModel,
        entry: NodeModEntry,
        launcher_urls: LauncherProviderUrls,
        providers: tuple[Provider, ...] | None = None,
        user: ModWebUser,
    ) -> LauncherMetadataResolution:
        if self._is_builtin_mod(entry):
            raise PermissionError("Built-in mod metadata cannot be fetched from mod web.")
        required_level = required_mod_mutation_level(NodeModMutationAction.UPDATE_PROPERTIES)
        if not self._user_has_level(user, required_level):
            raise PermissionError(f"{required_level.name.title()} access is required to fetch mod metadata.")
        payload = await self._remote_json_async(
            node=self._remote_node_link(model.node_name),
            app_name=model.app_name,
            path=(
                f"/apps/{quote(model.app_name, safe='')}/mods/{quote(entry.name, safe='')}/launcher-metadata"
            ),
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="POST",
            json_payload={
                "launcher_urls": launcher_urls.model_dump(mode="json"),
                **_launcher_provider_selection_payload(providers),
            },
        )
        return LauncherMetadataResolution.model_validate(payload)

    async def _resolve_mod_launcher_metadata(
        self,
        *,
        model: ModWebPageModel,
        entry: NodeModEntry,
        mod_pages: tuple[ModPageLink, ...],
        existing_launcher_urls: LauncherProviderUrls,
        providers: tuple[Provider, ...] | None = None,
        user: ModWebUser,
    ) -> LauncherMetadataDiscovery:
        if self._is_builtin_mod(entry):
            raise PermissionError("Built-in mod metadata cannot be resolved from mod web.")
        required_level = required_mod_mutation_level(NodeModMutationAction.UPDATE_PROPERTIES)
        if not self._user_has_level(user, required_level):
            raise PermissionError(f"{required_level.name.title()} access is required to resolve mod metadata.")
        payload = await self._remote_json_async(
            node=self._remote_node_link(model.node_name),
            app_name=model.app_name,
            path=(
                f"/apps/{quote(model.app_name, safe='')}/mods/{quote(entry.name, safe='')}"
                "/launcher-metadata/resolve"
            ),
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="POST",
            json_payload={
                "mod_pages": [page.model_dump(mode="json") for page in mod_pages],
                "existing_launcher_urls": existing_launcher_urls.model_dump(mode="json"),
                **_launcher_provider_selection_payload(providers),
            },
        )
        return LauncherMetadataDiscovery.model_validate(payload)

    async def _find_mod_pages(
        self,
        *,
        model: ModWebPageModel,
        entry: NodeModEntry,
        mod_pages: tuple[ModPageLink, ...],
        providers: tuple[Provider, ...] | None = None,
        user: ModWebUser,
    ) -> ModPageDiscovery:
        if self._is_builtin_mod(entry):
            raise PermissionError("Built-in mod pages cannot be resolved from mod web.")
        required_level = required_mod_mutation_level(NodeModMutationAction.UPDATE_PROPERTIES)
        if not self._user_has_level(user, required_level):
            raise PermissionError(f"{required_level.name.title()} access is required to resolve mod pages.")
        payload = await self._remote_json_async(
            node=self._remote_node_link(model.node_name),
            app_name=model.app_name,
            path=(
                f"/apps/{quote(model.app_name, safe='')}/mods/{quote(entry.name, safe='')}"
                "/mod-pages/resolve"
            ),
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="POST",
            json_payload={
                "mod_pages": [page.model_dump(mode="json") for page in mod_pages],
                **_launcher_provider_selection_payload(providers),
            },
        )
        return ModPageDiscovery.model_validate(payload)

    async def _discover_bulk_mod_metadata(
        self,
        *,
        model: ModWebPageModel,
        operation_id: str,
        mod_names: tuple[str, ...] = (),
        user: ModWebUser,
    ) -> BulkLauncherMetadataDiscovery:
        required_level = required_mod_mutation_level(NodeModMutationAction.UPDATE_PROPERTIES)
        if not self._user_has_level(user, required_level):
            raise PermissionError(f"{required_level.name.title()} access is required to resolve mod metadata.")
        payload = await self._remote_json_async(
            node=self._remote_node_link(model.node_name),
            app_name=model.app_name,
            path=f"/apps/{quote(model.app_name, safe='')}/mods/metadata/discover",
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="POST",
            json_payload={"operation_id": operation_id, "mod_names": list(mod_names)},
            timeout=_BULK_METADATA_REQUEST_TIMEOUT_SECONDS,
        )
        return BulkLauncherMetadataDiscovery.model_validate(payload)

    async def _apply_bulk_mod_metadata(
        self,
        *,
        model: ModWebPageModel,
        operation_id: str,
        discovery_operation_id: str,
        mod_names: tuple[str, ...],
        apply_suggested_type_mod_names: tuple[str, ...] = (),
        user: ModWebUser,
    ) -> NodeBulkLauncherMetadataApplyResult:
        if not mod_names:
            raise ValueError("Select at least one exact metadata match to apply.")
        required_level = required_mod_mutation_level(NodeModMutationAction.UPDATE_PROPERTIES)
        if not self._user_has_level(user, required_level):
            raise PermissionError(f"{required_level.name.title()} access is required to update mod metadata.")
        payload = await self._remote_json_async(
            node=self._remote_node_link(model.node_name),
            app_name=model.app_name,
            path=f"/apps/{quote(model.app_name, safe='')}/mods/metadata/apply",
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="POST",
            json_payload={
                "operation_id": operation_id,
                "discovery_operation_id": discovery_operation_id,
                "mod_names": list(mod_names),
                "apply_suggested_type_mod_names": list(apply_suggested_type_mod_names),
            },
            timeout=_BULK_METADATA_REQUEST_TIMEOUT_SECONDS,
        )
        return NodeBulkLauncherMetadataApplyResult.model_validate(payload)

    async def _cancel_bulk_mod_metadata(
        self,
        *,
        model: ModWebPageModel,
        operation_id: str,
        user: ModWebUser,
    ) -> bool:
        required_level = required_mod_mutation_level(NodeModMutationAction.UPDATE_PROPERTIES)
        if not self._user_has_level(user, required_level):
            raise PermissionError(f"{required_level.name.title()} access is required to cancel mod metadata.")
        payload = await self._remote_json_async(
            node=self._remote_node_link(model.node_name),
            app_name=model.app_name,
            path=(
                f"/apps/{quote(model.app_name, safe='')}/mods/metadata/"
                f"{quote(operation_id, safe='')}/cancel"
            ),
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="POST",
            json_payload={},
        )
        raw_cancelled = payload.get("cancelled")
        if not isinstance(raw_cancelled, bool):
            raise ValueError("Bulk metadata cancellation returned an invalid response.")
        return raw_cancelled

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
            case NodeModMutationAction.UPDATE_NOTES:
                return "Save Notes"
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
            case NodeModMutationAction.UPDATE_NOTES:
                return "mod-list-button"
            case NodeModMutationAction.DELETE:
                return "mod-list-button danger"
            case _:
                assert_never(action)

    @staticmethod
    def _is_builtin_mod(entry: NodeModEntry) -> bool:
        return entry.mod_type is ModType.BUILTIN

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
        factorio_chat_relay_use_shout: bool | None = None,
        rcon_requires_online_players: bool | None = None,
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
            factorio_chat_relay_use_shout=factorio_chat_relay_use_shout,
            rcon_requires_online_players=rcon_requires_online_players,
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

    async def _discord_settings(self, *, node_name: str, user: ModWebUser) -> config.DiscordSettings:
        self._require_user_level(user=user, required_level=Power_Level.sudo)
        node = self._remote_node_link(node_name)
        return await self._remote_discord_settings_async(node, user)

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

    async def _update_discord_settings(
        self,
        *,
        node_name: str,
        user: ModWebUser,
        settings: config.DiscordSettings,
    ) -> NodeDiscordSettingsMutationResult:
        self._require_user_level(user=user, required_level=Power_Level.sudo)
        node = self._remote_node_link(node_name)
        return await self._remote_update_discord_settings_async(node, settings, user)

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
        version_text: str = entry.version or "Unknown"
        download_text = self._mod_download_summary(entry)
        client_pack_text = self._mod_client_pack_summary(entry)
        available_actions = self._available_mod_actions(user=user, entry=entry)
        can_edit_properties: bool = not self._is_builtin_mod(entry) and self._user_has_level(
            user,
            required_mod_mutation_level(NodeModMutationAction.UPDATE_PROPERTIES),
        )
        can_edit_notes: bool = self._user_has_level(user, Power_Level.admin)
        supports_client_pack: bool = mod_capabilities_for_scope(model.app_scope).supports_client_pack
        launcher_metadata_providers = mod_capabilities_for_scope(model.app_scope).launcher_metadata_providers
        launcher_url_inputs: dict[Provider, Input] = {}
        curseforge_project_id_input: Input | None = None
        curseforge_file_id_input: Input | None = None
        metadata_suggestion_label: Label | None = None
        detect_metadata_status_label: Label | None = None
        find_mod_pages_button: Button | None = None
        resolve_launcher_metadata_button: Button | None = None
        fetch_launcher_metadata_button: Button | None = None
        detect_metadata_button: Button | None = None
        metadata_detection_save_button: Button | None = None
        save_properties_button: Button | None = None
        save_notes_button: Button | None = None
        notes_input: Textarea | None = None
        check_update_button: Button | None = None
        check_update_version_input: Input | None = None
        check_update_version_select: Select | None = None
        check_update_versions_button: Button | None = None
        check_update_versions_status_label: Label | None = None
        check_update_status_label: Label | None = None
        mod_page_rows: list[_ModPageEditorRow] = []
        mod_pages_container: Column | None = None
        mod_page_resolution_dialog: Dialog
        mod_page_resolution_selects: dict[Provider, Select] = {}
        mod_page_resolution_single_urls: dict[Provider, str] = {}
        mod_page_resolution_pages: dict[str, ModPageLink] = {}
        mod_page_resolution_on_confirm: Callable[[], Awaitable[None]] | None = None
        launcher_resolution_dialog: Dialog
        launcher_resolution_selects: dict[Provider, Select] = {}
        launcher_resolution_single_urls: dict[Provider, str] = {}
        launcher_resolution_on_confirm: Callable[[], Awaitable[None]] | None = None
        metadata_detection_review_dialog: Dialog
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
        can_check_update: bool = (
            model.app_scope == config.AppScopes.factorio.value
            and entry.placement is ModPlacement.SERVER_ENABLED
            and self._user_has_level(user, Power_Level.user)
        )
        cached_update_result = self._cached_mod_update_result(model=model, entry=entry) if can_check_update else None
        available_update_result: NodeModUpdateCheckResult | None = self._actionable_mod_update_result(
            cached_update_result
        )
        check_update_versions: NodeModPortalVersionList | None = None
        active_metadata_panel: Literal["overrides", "launcher"] | None = None

        async def save_notes() -> None:
            if save_notes_button is None or notes_input is None:
                raise RuntimeError("Save Notes button was not rendered.")
            try:
                notes = str(notes_input.value or "").strip() or None
                result = await self._update_mod_notes(model=model, entry=entry, notes=notes, user=user)
            except Exception as xcp:
                log.warning(
                    "Mod notes update failed: node=%s app=%s mod=%s error=%s",
                    model.node_name,
                    model.app_name,
                    entry.name,
                    xcp,
                )
                ui.notify(f"Mod notes update failed: {xcp}", type="negative")
                return
            dialog.close()
            ui.notify(result.message, type="positive")
            self._guarded_reload(ui=ui)

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

        def launcher_urls_from_inputs() -> LauncherProviderUrls:
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
            return LauncherProviderUrls(
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

        def remove_mod_page_row(editor_row: _ModPageEditorRow) -> None:
            mod_page_rows.remove(editor_row)
            editor_row.container.delete()

        def mod_page_url_validation(raw_url: str) -> str | None:
            if not raw_url.strip():
                return None
            try:
                normalise_mod_page_url(raw_url)
            except (TypeError, ValueError):
                return "Enter an absolute HTTPS URL."
            return None

        def refresh_mod_page_row(
            editor_row: _ModPageEditorRow,
            *,
            raw_url: str | None = None,
        ) -> None:
            resolved_url = (
                _value_as_text(editor_row.url_input).strip()
                if raw_url is None
                else raw_url.strip()
            )
            validation_error = mod_page_url_validation(resolved_url)
            if validation_error is None:
                editor_row.url_input.props('label="URL"')
                editor_row.url_input.classes(remove="mod-page-url-invalid")
            else:
                editor_row.url_input.props(f'label="URL — {validation_error}"')
                editor_row.url_input.classes(add="mod-page-url-invalid")
            try:
                normalise_mod_page_url(resolved_url)
            except (TypeError, ValueError):
                valid_url = False
            else:
                valid_url = True

            provider = known_mod_page_provider_for_url(resolved_url)
            if provider is not None:
                canonical_name = provider.value
                editor_row.name_input.set_value(canonical_name)
                editor_row.automatic_name = canonical_name
                editor_row.name_input.set_enabled(False)
                return

            current_name = _value_as_text(editor_row.name_input)
            if editor_row.automatic_name is not None and current_name == editor_row.automatic_name:
                editor_row.name_input.set_value("")
            editor_row.automatic_name = None
            editor_row.name_input.set_enabled(valid_url)

        def add_mod_page_row(page: ModPageLink | None = None) -> None:
            if mod_pages_container is None:
                raise RuntimeError("Mod page editor was not rendered.")
            with mod_pages_container:
                with ui.column().classes("w-full gap-2") as row_container:
                    url_input = (
                        ui.input(
                            "URL",
                            value="" if page is None else page.url,
                            placeholder="https://…",
                        )
                        .props("filled square dense clearable hide-bottom-space color=accent")
                        .classes("w-full mod-app-details-field")
                    )
                    with ui.row().classes("w-full gap-2 items-end mod-page-editor-controls"):
                        name_input = (
                            ui.input(
                                "Name",
                                value="" if page is None else page.name,
                                placeholder="Provider or page name",
                            )
                            .props("filled square dense clearable hide-bottom-space color=accent maxlength=80")
                            .classes("mod-app-details-field grow")
                        )
                        remove_button = ui.button("Remove").classes("mod-list-button secondary")
            editor_row = _ModPageEditorRow(
                container=row_container,
                name_input=name_input,
                url_input=url_input,
            )
            mod_page_rows.append(editor_row)
            remove_button.on_click(lambda: remove_mod_page_row(editor_row))
            url_input.on_value_change(
                lambda event: refresh_mod_page_row(
                    editor_row,
                    raw_url=str(event.value or ""),
                )
            )
            url_input.on("keydown.enter", lambda: refresh_mod_page_row(editor_row))
            refresh_mod_page_row(editor_row)

        def add_mod_page_row_if_missing(page: ModPageLink) -> None:
            for editor_row in mod_page_rows:
                try:
                    existing_url = normalise_mod_page_url(_value_as_text(editor_row.url_input))
                except (TypeError, ValueError):
                    continue
                if existing_url == page.url:
                    return
            add_mod_page_row(page)

        def mod_pages_from_inputs() -> tuple[ModPageLink, ...]:
            pages: list[ModPageLink] = []
            for row_number, editor_row in enumerate(mod_page_rows, start=1):
                name = _value_as_text(editor_row.name_input).strip()
                url = _value_as_text(editor_row.url_input).strip()
                if not name and not url:
                    continue
                if not name or not url:
                    raise ValueError(f"Mod page row {row_number} requires both a name and URL.")
                pages.append(ModPageLink(name=name, url=url))
            return tuple(pages)

        async def confirm_mod_page_resolution() -> None:
            selected_urls = dict(mod_page_resolution_single_urls)
            for provider, selection in mod_page_resolution_selects.items():
                selected_url = _value_as_text(selection).strip()
                if not selected_url:
                    ui.notify(
                        f"Select a {launcher_provider_label(provider)} project page.",
                        type="negative",
                    )
                    return
                selected_urls[provider] = selected_url
            for selected_url in selected_urls.values():
                page = mod_page_resolution_pages.get(selected_url)
                if page is None:
                    raise RuntimeError("Selected mod page candidate is unavailable.")
                add_mod_page_row_if_missing(page)
            mod_page_resolution_dialog.close()
            if mod_page_resolution_on_confirm is not None:
                await mod_page_resolution_on_confirm()
                return
            ui.notify("Mod pages added. Save the mod properties to persist them.", type="positive")

        def present_mod_page_resolution(
            discovery: ModPageDiscovery,
            *,
            on_confirm: Callable[[], Awaitable[None]] | None = None,
        ) -> None:
            nonlocal mod_page_resolution_on_confirm
            mod_page_resolution_on_confirm = on_confirm
            mod_page_resolution_dialog.clear()
            mod_page_resolution_selects.clear()
            mod_page_resolution_single_urls.clear()
            mod_page_resolution_pages.clear()
            with mod_page_resolution_dialog:
                with ui.card().classes("mod-card mod-dialog-card mod-app-details-card"):
                    ui.label("Find mod pages").classes("mod-card-title")
                    ui.label(
                        "Confirm the provider project pages found from the local mod. "
                        "They will remain unsaved until you save the mod properties."
                    ).classes("mod-subtitle text-xs")
                    with ui.column().classes("w-full gap-3"):
                        for provider_result in discovery.providers:
                            provider_label = launcher_provider_label(provider_result.provider)
                            with ui.column().classes("w-full mod-metadata-review-provider"):
                                ui.label(provider_label).classes(
                                    "mod-metadata-review-provider-title"
                                )
                                if provider_result.error is not None:
                                    ui.label(provider_result.error).classes(
                                        "mod-subtitle text-xs text-negative"
                                    )
                                    continue
                                if not provider_result.candidates:
                                    ui.label("No matching projects found.").classes(
                                        "mod-subtitle text-xs"
                                    )
                                    continue
                                options = {
                                    candidate.page.url: candidate.selection_label
                                    for candidate in provider_result.candidates
                                }
                                for candidate in provider_result.candidates:
                                    mod_page_resolution_pages[candidate.page.url] = candidate.page
                                first_url = provider_result.candidates[0].page.url
                                if len(provider_result.candidates) == 1:
                                    mod_page_resolution_single_urls[
                                        provider_result.provider
                                    ] = first_url
                                    ui.label(
                                        provider_result.candidates[0].selection_label
                                    ).classes("mod-subtitle text-xs")
                                    continue
                                mod_page_resolution_selects[provider_result.provider] = (
                                    ui.select(
                                        options,
                                        value=first_url,
                                        label="Project page",
                                    )
                                    .props(
                                        "filled square dense hide-bottom-space color=accent options-dark"
                                    )
                                    .classes("w-full mod-app-details-field")
                                )
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("Cancel", on_click=mod_page_resolution_dialog.close).classes(
                            "mod-list-button secondary"
                        )
                        ui.button("Confirm", on_click=confirm_mod_page_resolution).classes(
                            "mod-list-button"
                        )
            mod_page_resolution_dialog.open()

        async def _find_mod_pages_from_local_data(
            providers: tuple[Provider, ...] | None = None,
        ) -> None:
            try:
                discovery = await self._find_mod_pages(
                    model=model,
                    entry=entry,
                    mod_pages=mod_pages_from_inputs(),
                    providers=providers,
                    user=user,
                )
            except Exception as xcp:
                log.warning(
                    "Mod page discovery failed: node=%s app=%s mod=%s error=%s",
                    model.node_name,
                    model.app_name,
                    entry.name,
                    xcp,
                )
                ui.notify(f"Mod page discovery failed: {xcp}", type="negative")
                return
            if not discovery.candidates:
                details = "; ".join(
                    (
                        f"{launcher_provider_label(result.provider)}: {result.error}"
                        if result.error is not None
                        else f"{launcher_provider_label(result.provider)}: no matching projects"
                    )
                    for result in discovery.providers
                )
                ui.notify(
                    f"No matching mod pages found. {details}",
                    type="warning",
                    multi_line=True,
                )
                return
            present_mod_page_resolution(discovery)

        async def find_mod_pages_from_local_data(
            providers: tuple[Provider, ...] | None = None,
        ) -> None:
            if find_mod_pages_button is None:
                raise RuntimeError("Find Mod Pages button was not rendered.")
            await self._run_with_loading_button(
                button=find_mod_pages_button,
                action=lambda: _find_mod_pages_from_local_data(providers),
            )

        def ensure_launcher_mod_page_rows(launcher_urls: LauncherProviderUrls) -> None:
            existing_providers = {
                provider
                for editor_row in mod_page_rows
                if (
                    provider := known_mod_page_provider_for_url(
                        _value_as_text(editor_row.url_input).strip()
                    )
                )
                is not None
            }
            provider_mapping = (
                (Provider.MODRINTH, KnownModPageProvider.MODRINTH),
                (Provider.CURSEFORGE, KnownModPageProvider.CURSEFORGE),
            )
            for launcher_provider, page_provider in provider_mapping:
                page_url = launcher_urls.for_provider(launcher_provider)
                if page_url is None or page_provider in existing_providers:
                    continue
                try:
                    project_page_url = launcher_project_page_url(page_url, launcher_provider)
                except ValueError:
                    continue
                add_mod_page_row_if_missing(
                    ModPageLink(
                        name=page_provider.value,
                        url=project_page_url,
                    )
                )
                existing_providers.add(page_provider)

        def apply_launcher_metadata_resolution(resolution: LauncherMetadataResolution) -> None:
            if metadata_suggestion_label is None:
                raise RuntimeError("Metadata suggestion label was not rendered.")
            if resolution.suggested_mod_type is None:
                metadata_suggestion_label.set_text(
                    "Suggested type: unavailable from the supplied provider metadata."
                )
            else:
                assert resolution.suggestion_provider is not None
                metadata_suggestion_label.set_text(
                    f"Suggested type: {resolution.suggested_mod_type.label} "
                    f"({launcher_provider_label(resolution.suggestion_provider)})"
                )

        def launcher_metadata_error_summary(resolution: LauncherMetadataResolution) -> str:
            return "; ".join(
                f"{launcher_provider_label(error.provider)}: {error.message}"
                for error in resolution.provider_errors
            )

        async def _fetch_launcher_metadata(
            providers: tuple[Provider, ...] | None = None,
        ) -> None:
            try:
                launcher_urls = launcher_urls_from_inputs()
                ensure_launcher_mod_page_rows(launcher_urls)
                resolution = await self._fetch_mod_launcher_metadata(
                    model=model,
                    entry=entry,
                    launcher_urls=launcher_urls,
                    providers=providers,
                    user=user,
                )
            except Exception as xcp:
                log.warning(
                    "Mod launcher metadata fetch failed: node=%s app=%s mod=%s error=%s",
                    model.node_name,
                    model.app_name,
                    entry.name,
                    xcp,
                )
                ui.notify(f"Metadata fetch failed: {xcp}", type="negative")
                return
            apply_launcher_metadata_resolution(resolution)
            if resolution.provider_errors:
                ui.notify(
                    "Metadata fetched from available providers. "
                    f"Unavailable providers: {launcher_metadata_error_summary(resolution)}",
                    type="warning",
                    multi_line=True,
                )
            else:
                ui.notify("Launcher metadata fetched.", type="positive")

        async def fetch_launcher_metadata(
            providers: tuple[Provider, ...] | None = None,
        ) -> None:
            if fetch_launcher_metadata_button is None:
                raise RuntimeError("Fetch Metadata button was not rendered.")
            await self._run_with_loading_button(
                button=fetch_launcher_metadata_button,
                action=lambda: _fetch_launcher_metadata(providers),
            )

        async def confirm_launcher_resolution() -> None:
            selected_urls = dict(launcher_resolution_single_urls)
            for provider, selection in launcher_resolution_selects.items():
                selected_url = _value_as_text(selection).strip()
                if not selected_url:
                    ui.notify(
                        f"Select a {launcher_provider_label(provider)} file page.",
                        type="negative",
                    )
                    return
                selected_urls[provider] = selected_url

            for provider, selected_url in selected_urls.items():
                launcher_input = launcher_url_inputs.get(provider)
                if launcher_input is None:
                    raise RuntimeError(
                        f"{launcher_provider_label(provider)} file-page input was not rendered."
                    )
                launcher_input.set_value(selected_url)
            launcher_resolution_dialog.close()
            if launcher_resolution_on_confirm is not None:
                await launcher_resolution_on_confirm()
                return
            await fetch_launcher_metadata()

        def present_launcher_resolution(
            discovery: LauncherMetadataDiscovery,
            *,
            on_confirm: Callable[[], Awaitable[None]] | None = None,
        ) -> None:
            nonlocal launcher_resolution_on_confirm
            launcher_resolution_on_confirm = on_confirm
            launcher_resolution_dialog.clear()
            launcher_resolution_selects.clear()
            launcher_resolution_single_urls.clear()
            with launcher_resolution_dialog:
                with ui.card().classes("mod-card mod-dialog-card mod-app-details-card"):
                    ui.label("Resolve launcher metadata").classes("mod-card-title")
                    ui.label(
                        "Confirm the provider file pages found for the local mod. "
                        "They will remain unsaved until you save the mod properties."
                    ).classes("mod-subtitle text-xs")
                    with ui.column().classes("w-full gap-3"):
                        for provider_result in discovery.providers:
                            provider_label = launcher_provider_label(provider_result.provider)
                            with ui.column().classes("w-full mod-metadata-review-provider"):
                                ui.label(provider_label).classes(
                                    "mod-metadata-review-provider-title"
                                )
                                if provider_result.error is not None:
                                    ui.label(provider_result.error).classes(
                                        "mod-subtitle text-xs text-negative"
                                    )
                                    continue
                                if not provider_result.candidates:
                                    ui.label("No matching files found.").classes(
                                        "mod-subtitle text-xs"
                                    )
                                    continue
                                options = {
                                    candidate.file_page_url: candidate.selection_label
                                    for candidate in provider_result.candidates
                                }
                                first_url = provider_result.candidates[0].file_page_url
                                if len(provider_result.candidates) == 1:
                                    launcher_resolution_single_urls[
                                        provider_result.provider
                                    ] = first_url
                                    ui.label(
                                        provider_result.candidates[0].selection_label
                                    ).classes("mod-subtitle text-xs")
                                    continue
                                launcher_resolution_selects[provider_result.provider] = (
                                    ui.select(
                                        options,
                                        value=first_url,
                                        label="File page",
                                    )
                                    .props(
                                        "filled square dense hide-bottom-space color=accent options-dark"
                                    )
                                    .classes("w-full mod-app-details-field")
                                )
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("Cancel", on_click=launcher_resolution_dialog.close).classes(
                            "mod-list-button secondary"
                        )
                        ui.button("Confirm", on_click=confirm_launcher_resolution).classes(
                            "mod-list-button"
                        )
            launcher_resolution_dialog.open()

        async def _resolve_launcher_metadata_from_mod_pages(
            providers: tuple[Provider, ...] | None = None,
        ) -> None:
            try:
                discovery = await self._resolve_mod_launcher_metadata(
                    model=model,
                    entry=entry,
                    mod_pages=mod_pages_from_inputs(),
                    existing_launcher_urls=launcher_urls_from_inputs(),
                    providers=providers,
                    user=user,
                )
            except Exception as xcp:
                log.warning(
                    "Mod launcher metadata resolution failed: node=%s app=%s mod=%s error=%s",
                    model.node_name,
                    model.app_name,
                    entry.name,
                    xcp,
                )
                ui.notify(f"Metadata resolution failed: {xcp}", type="negative")
                return
            if not discovery.candidates:
                details = "; ".join(
                    (
                        f"{launcher_provider_label(result.provider)}: {result.error}"
                        if result.error is not None
                        else f"{launcher_provider_label(result.provider)}: no matching files"
                    )
                    for result in discovery.providers
                )
                ui.notify(
                    f"No launcher metadata matches found. {details}",
                    type="warning",
                    multi_line=True,
                )
                return
            selected_urls = self._automatic_launcher_urls(discovery)
            if selected_urls:
                apply_launcher_urls(selected_urls)
                provider_errors = tuple(
                    result for result in discovery.providers if result.error is not None
                )
                if provider_errors:
                    details = "; ".join(
                        f"{launcher_provider_label(result.provider)}: {result.error}"
                        for result in provider_errors
                    )
                    ui.notify(
                        f"Resolved available provider metadata. Unavailable providers: {details}",
                        type="warning",
                        multi_line=True,
                    )
                await _fetch_launcher_metadata(providers)
                return
            present_launcher_resolution(
                discovery,
                on_confirm=lambda: _fetch_launcher_metadata(providers),
            )

        async def resolve_launcher_metadata_from_mod_pages(
            providers: tuple[Provider, ...] | None = None,
        ) -> None:
            if resolve_launcher_metadata_button is None:
                raise RuntimeError("Resolve from Mod Pages button was not rendered.")
            await self._run_with_loading_button(
                button=resolve_launcher_metadata_button,
                action=lambda: _resolve_launcher_metadata_from_mod_pages(providers),
            )

        def set_detection_status(message: str) -> None:
            if detect_metadata_status_label is None:
                raise RuntimeError("Metadata detection status was not rendered.")
            detect_metadata_status_label.set_text(message)

        def apply_launcher_urls(selected_urls: dict[Provider, str]) -> None:
            for provider, selected_url in selected_urls.items():
                launcher_input = launcher_url_inputs.get(provider)
                if launcher_input is None:
                    raise RuntimeError(
                        f"{launcher_provider_label(provider)} file-page input was not rendered."
                    )
                launcher_input.set_value(selected_url)

        def present_metadata_detection_review(resolution: LauncherMetadataResolution) -> None:
            nonlocal metadata_detection_save_button
            metadata_detection_review_dialog.clear()
            pages = mod_pages_from_inputs()
            launcher_urls = launcher_urls_from_inputs()
            known_provider_by_launcher = {
                Provider.MODRINTH: KnownModPageProvider.MODRINTH,
                Provider.CURSEFORGE: KnownModPageProvider.CURSEFORGE,
            }
            provider_errors = {
                error.provider: error.message for error in resolution.provider_errors
            }
            with metadata_detection_review_dialog:
                with ui.card().classes(
                    "mod-card mod-dialog-card mod-app-details-card mod-metadata-review-card"
                ):
                    ui.label("Metadata detected").classes("mod-card-title")
                    ui.label(
                        "Review the detected values below. Nothing is persisted until you save the mod properties."
                    ).classes("mod-subtitle text-xs")
                    with ui.column().classes("w-full mod-metadata-review-summary"):
                        ui.label("Suggested type").classes("mod-metadata-review-field-label")
                        suggestion = (
                            "Unavailable"
                            if resolution.suggested_mod_type is None
                            else resolution.suggested_mod_type.label
                        )
                        ui.label(suggestion).classes("mod-metadata-review-suggestion")
                    with ui.column().classes("w-full mod-metadata-review-providers"):
                        for provider in launcher_metadata_providers:
                            known_provider = known_provider_by_launcher[provider]
                            project_page = next(
                                (
                                    page
                                    for page in pages
                                    if known_mod_page_provider_for_url(page.url) is known_provider
                                ),
                                None,
                            )
                            file_page_url = launcher_urls.for_provider(provider)
                            reference = (
                                launcher_urls.curseforge_reference
                                if provider is Provider.CURSEFORGE
                                else None
                            )
                            if project_page is None and file_page_url is None and reference is None:
                                continue
                            with ui.column().classes("w-full mod-metadata-review-provider"):
                                ui.label(launcher_provider_label(provider)).classes(
                                    "mod-metadata-review-provider-title"
                                )
                                if error_message := provider_errors.get(provider):
                                    ui.label(error_message).classes(
                                        "mod-subtitle text-xs text-negative"
                                    )
                                if project_page is not None:
                                    ui.label("Project page").classes(
                                        "mod-metadata-review-field-label"
                                    )
                                    ui.link(
                                        project_page.url,
                                        project_page.url,
                                        new_tab=True,
                                    ).props('rel="noopener noreferrer"').classes(
                                        "mod-metadata-review-link"
                                    )
                                if file_page_url is not None:
                                    ui.label("File page").classes(
                                        "mod-metadata-review-field-label"
                                    )
                                    ui.link(
                                        file_page_url,
                                        file_page_url,
                                        new_tab=True,
                                    ).props('rel="noopener noreferrer"').classes(
                                        "mod-metadata-review-link"
                                    )
                                if reference is not None:
                                    ui.label("File reference").classes(
                                        "mod-metadata-review-field-label"
                                    )
                                    ui.label(
                                        f"Project {reference.project_id} · File {reference.file_id}"
                                    ).classes("mod-metadata-review-reference")
                    with ui.row().classes(
                        "w-full justify-end gap-2 mod-metadata-review-actions"
                    ):
                        ui.button(
                            "Continue Editing",
                            on_click=metadata_detection_review_dialog.close,
                        ).classes("mod-list-button secondary")
                        metadata_detection_save_button = ui.button(
                            "Save Changes",
                            on_click=save_detected_metadata,
                        ).classes("mod-list-button")
            metadata_detection_review_dialog.open()

        async def finish_metadata_detection(
            providers: tuple[Provider, ...] | None = None,
        ) -> None:
            set_detection_status("Fetching provider metadata…")
            launcher_urls = launcher_urls_from_inputs()
            ensure_launcher_mod_page_rows(launcher_urls)
            resolution = await self._fetch_mod_launcher_metadata(
                model=model,
                entry=entry,
                launcher_urls=launcher_urls,
                providers=providers,
                user=user,
            )
            apply_launcher_metadata_resolution(resolution)
            if resolution.provider_errors:
                set_detection_status(
                    "Detection complete with unavailable provider metadata. Review the details."
                )
            else:
                set_detection_status("Detection complete. Review the detected values.")
            present_metadata_detection_review(resolution)

        async def resolve_launcher_metadata_for_detection(
            providers: tuple[Provider, ...] | None = None,
        ) -> None:
            set_detection_status("Resolving matching launcher files…")
            discovery = await self._resolve_mod_launcher_metadata(
                model=model,
                entry=entry,
                mod_pages=mod_pages_from_inputs(),
                existing_launcher_urls=launcher_urls_from_inputs(),
                providers=providers,
                user=user,
            )
            if not discovery.candidates:
                if any(
                    launcher_urls_from_inputs().has_provider(provider)
                    for provider in launcher_metadata_providers
                ):
                    await finish_metadata_detection(providers)
                    return
                raise ValueError("No matching launcher files were found for the detected mod pages.")
            selected_urls = self._automatic_launcher_urls(discovery)
            if selected_urls is None:
                set_detection_status("Choose the matching launcher files to continue.")
                present_launcher_resolution(
                    discovery,
                    on_confirm=lambda: resume_detection_after_launcher_selection(providers),
                )
                return
            apply_launcher_urls(selected_urls)
            await finish_metadata_detection(providers)

        async def discover_mod_pages_for_detection(
            providers: tuple[Provider, ...] | None = None,
        ) -> None:
            set_detection_status("Finding matching project pages…")
            discovery = await self._find_mod_pages(
                model=model,
                entry=entry,
                mod_pages=mod_pages_from_inputs(),
                providers=providers,
                user=user,
            )
            if not discovery.candidates:
                if any(
                    launcher_urls_from_inputs().has_provider(provider)
                    for provider in launcher_metadata_providers
                ):
                    await finish_metadata_detection(providers)
                    return
                raise ValueError("No matching project pages were found for the local mod.")
            selected_pages = self._automatic_mod_pages(discovery)
            if selected_pages is None:
                set_detection_status("Choose the matching project pages to continue.")
                present_mod_page_resolution(
                    discovery,
                    on_confirm=lambda: resume_detection_after_mod_page_selection(providers),
                )
                return
            for page in selected_pages:
                add_mod_page_row_if_missing(page)
            await resolve_launcher_metadata_for_detection(providers)

        async def run_metadata_detection_action(
            action: Callable[[], Awaitable[None]],
        ) -> None:
            if detect_metadata_button is None:
                raise RuntimeError("Detect Metadata button was not rendered.")

            async def guarded_action() -> None:
                try:
                    await action()
                except Exception as xcp:
                    log.warning(
                        "Automatic mod metadata detection failed: node=%s app=%s mod=%s error=%s",
                        model.node_name,
                        model.app_name,
                        entry.name,
                        xcp,
                    )
                    set_detection_status("Detection failed. Use Advanced Actions for manual recovery.")
                    ui.notify(f"Metadata detection failed: {xcp}", type="negative")

            await self._run_with_loading_button(
                button=detect_metadata_button,
                action=guarded_action,
            )

        async def detect_metadata(
            providers: tuple[Provider, ...] | None = None,
        ) -> None:
            async def start_detection() -> None:
                launcher_urls = launcher_urls_from_inputs()
                mod_pages = mod_pages_from_inputs()
                providers_missing_pages = self._launcher_providers_missing_mod_pages(
                    providers=(launcher_metadata_providers if providers is None else providers),
                    launcher_urls=launcher_urls,
                    mod_pages=mod_pages,
                )
                if providers_missing_pages:
                    await discover_mod_pages_for_detection(providers)
                    return
                if any(
                    not launcher_urls.has_provider(provider)
                    for provider in (launcher_metadata_providers if providers is None else providers)
                ):
                    await resolve_launcher_metadata_for_detection(providers)
                    return
                await finish_metadata_detection(providers)

            await run_metadata_detection_action(start_detection)

        async def resume_detection_after_mod_page_selection(
            providers: tuple[Provider, ...] | None = None,
        ) -> None:
            await run_metadata_detection_action(
                lambda: resolve_launcher_metadata_for_detection(providers)
            )

        async def resume_detection_after_launcher_selection(
            providers: tuple[Provider, ...] | None = None,
        ) -> None:
            await run_metadata_detection_action(lambda: finish_metadata_detection(providers))

        def add_provider_context_menu(
            *,
            button: Button,
            action: Callable[[tuple[Provider, ...] | None], Awaitable[None]],
        ) -> None:
            context_menu_factory = getattr(ui, "context_menu", None)
            menu_item_factory = getattr(ui, "menu_item", None)
            if not callable(context_menu_factory) or not callable(menu_item_factory):
                return

            def provider_action(provider: Provider) -> Callable[[], Awaitable[None]]:
                async def run() -> None:
                    await action((provider,))

                return run

            with button:
                with ui.context_menu().classes("mod-chat-entry-menu"):
                    for provider in launcher_metadata_providers:
                        ui.menu_item(
                            launcher_provider_label(provider),
                            on_click=provider_action(provider),
                        ).classes("mod-chat-entry-menu-item")

        async def save_detected_metadata() -> None:
            if metadata_detection_save_button is None:
                raise RuntimeError("Metadata detection Save Changes button was not rendered.")
            await self._run_with_loading_button(
                button=metadata_detection_save_button,
                action=_save_properties,
            )

        async def _save_properties() -> None:
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
                launcher_urls = launcher_urls_from_inputs()
                ensure_launcher_mod_page_rows(launcher_urls)
                result = await self._update_mod_properties(
                    model=model,
                    entry=entry,
                    mod_type=selected_mod_type,
                    download_block_reason=selected_block_reason,
                    metadata_overrides=metadata_overrides,
                    mod_pages=mod_pages_from_inputs(),
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
            self._guarded_reload(ui=ui)

        async def save_properties() -> None:
            if save_properties_button is None:
                raise RuntimeError("Save button was not rendered.")
            await self._run_with_loading_button(
                button=save_properties_button,
                action=_save_properties,
            )

        def update_check_status_text(result: NodeModUpdateCheckResult) -> str:
            blocked_dependencies = tuple(
                dependency
                for dependency in result.dependencies
                if dependency.action is NodeModUpdateDependencyAction.BLOCKED
            )
            if blocked_dependencies:
                details = ", ".join(
                    f"{dependency.title} ({dependency.block_reason or 'blocked'})"
                    for dependency in blocked_dependencies
                )
                return f"Blocked dependencies: {details}"
            pending_dependency_count = sum(
                1
                for dependency in result.dependencies
                if dependency.action
                in {
                    NodeModUpdateDependencyAction.INSTALL,
                    NodeModUpdateDependencyAction.UPDATE,
                }
            )
            dependency_suffix = (
                ""
                if pending_dependency_count == 0
                else f"; includes {pending_dependency_count} required dependenc{'ies' if pending_dependency_count != 1 else 'y'}"
            )
            if result.status is NodeModUpdateStatus.CURRENT:
                return f"Current: {result.latest_version}{dependency_suffix}"
            if result.status is NodeModUpdateStatus.UNKNOWN_CURRENT:
                return f"Latest: {result.latest_version}; local version unknown{dependency_suffix}"
            if result.status is NodeModUpdateStatus.UPDATE_AVAILABLE:
                return f"Update available{dependency_suffix}."
            assert_never(result.status)

        def update_check_button_text(result: NodeModUpdateCheckResult) -> str:
            return f"{result.current_version or 'unknown'} -> {result.latest_version}"

        def mod_portal_version_option_label(version: NodeModPortalVersionEntry) -> str:
            return version.version

        def selected_update_version() -> str | None:
            if check_update_version_select is not None:
                selected_version = _value_as_text(check_update_version_select).strip()
                return selected_version or None
            if check_update_version_input is None:
                return None
            version = _value_as_text(check_update_version_input).strip()
            return version or None

        def reset_update_check_state(_event: object | None = None) -> None:
            nonlocal available_update_result
            available_update_result = None
            if check_update_button is not None:
                check_update_button.set_text("Check Update")
            if check_update_status_label is not None:
                check_update_status_label.set_text("Not checked")

        async def _load_update_versions() -> None:
            nonlocal check_update_versions
            if check_update_versions_status_label is None:
                raise RuntimeError("Mod update versions status label was not rendered.")
            try:
                loaded_versions = await self._mod_versions(
                    model=model,
                    entry=entry,
                    user=user,
                )
            except Exception as xcp:
                check_update_versions = None
                check_update_versions_status_label.set_text("Version lookup failed.")
                ui.notify(f"Mod version lookup failed: {xcp}", type="negative", multi_line=True)
                check_update_version_control.refresh()
                return
            check_update_versions = loaded_versions
            reset_update_check_state()
            check_update_versions_status_label.set_text(
                f"{len(loaded_versions.versions)} compatible version"
                f"{'s' if len(loaded_versions.versions) != 1 else ''}."
            )
            check_update_version_control.refresh()

        async def load_update_versions() -> None:
            if check_update_versions_button is None:
                raise RuntimeError("Mod update versions button was not rendered.")
            await self._run_with_loading_button(
                button=check_update_versions_button,
                action=_load_update_versions,
            )

        async def _run_update_check() -> None:
            nonlocal available_update_result
            if check_update_status_label is None:
                raise RuntimeError("Update status label was not rendered.")
            if check_update_button is None:
                raise RuntimeError("Check Update button was not rendered.")
            try:
                result = await self._check_mod_update(
                    model=model,
                    entry=entry,
                    user=user,
                    version=selected_update_version(),
                )
            except Exception as xcp:
                log.warning(
                    "Mod update check failed: node=%s app=%s mod=%s error=%s",
                    model.node_name,
                    model.app_name,
                    entry.name,
                    xcp,
                )
                available_update_result = None
                check_update_button.set_text("Check Update")
                check_update_status_label.set_text("Update check failed.")
                ui.notify(f"Mod update check failed: {xcp}", type="negative")
                return
            has_blocked_dependencies = any(
                dependency.action is NodeModUpdateDependencyAction.BLOCKED
                for dependency in result.dependencies
            )
            available_update_result = self._actionable_mod_update_result(result)
            check_update_button.set_text(
                update_check_button_text(result)
                if available_update_result is not None
                else "Check Update"
            )
            check_update_status_label.set_text(update_check_status_text(result))
            has_dependency_work = any(
                dependency.action
                in {
                    NodeModUpdateDependencyAction.BLOCKED,
                    NodeModUpdateDependencyAction.INSTALL,
                    NodeModUpdateDependencyAction.UPDATE,
                }
                for dependency in result.dependencies
            )
            notify_type: Literal["positive", "warning"] = (
                "warning"
                if result.status is NodeModUpdateStatus.UPDATE_AVAILABLE or has_dependency_work
                else "positive"
            )
            ui.notify(result.message, type=notify_type)

        async def _run_mod_update() -> None:
            nonlocal available_update_result
            try:
                result = await self._update_mod(
                    model=model,
                    entry=entry,
                    user=user,
                    version=selected_update_version(),
                )
            except Exception as xcp:
                log.warning(
                    "Mod update failed: node=%s app=%s mod=%s error=%s",
                    model.node_name,
                    model.app_name,
                    entry.name,
                    xcp,
                )
                ui.notify(f"Mod update failed: {xcp}", type="negative")
                return
            available_update_result = None
            dialog.close()
            ui.notify(result.message, type="positive")
            self._guarded_reload(ui=ui)

        async def check_update() -> None:
            if check_update_button is None:
                raise RuntimeError("Check Update button was not rendered.")
            await self._run_with_loading_button(
                button=check_update_button,
                action=_run_mod_update if available_update_result is not None else _run_update_check,
            )

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
            self._guarded_reload(ui=ui)

        async def _confirm_delete() -> None:
            await run_mod_action(NodeModMutationAction.DELETE)
            delete_confirm_dialog.close()

        async def confirm_delete() -> None:
            if delete_confirm_button is None:
                raise RuntimeError("Delete button was not rendered.")
            await self._run_with_loading_button(
                button=delete_confirm_button,
                action=_confirm_delete,
            )

        def _create_mod_action_handler(
            action: NodeModMutationAction,
        ) -> Callable[[object | None], Awaitable[None]]:
            async def _handle_mod_action(_: object | None = None) -> None:
                await run_mod_action(action)

            return _handle_mod_action

        mod_page_resolution_dialog = ui.dialog()
        launcher_resolution_dialog = ui.dialog()
        metadata_detection_review_dialog = ui.dialog()
        delete_confirm_button: Button | None = None

        with ui.dialog() as delete_confirm_dialog:
            with ui.card().classes("mod-card mod-dialog-card"):
                with ui.column().classes("w-full gap-4 p-5"):
                    ui.label("Delete mod?").classes("text-xl font-black mod-title-small")
                    ui.label(f"{entry.friendly} will be removed from the server.").classes("mod-subtitle text-sm")
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("Cancel", on_click=delete_confirm_dialog.close).classes("mod-list-button secondary")
                        delete_confirm_button = ui.button(
                            "Delete",
                            on_click=confirm_delete,
                        ).classes("mod-list-button danger")

        with ui.dialog() as dialog:
            with ui.card().classes(
                "mod-card mod-dialog-card mod-app-details-dialog-card mod-mod-details-dialog-card"
            ):
                with ui.column().classes("w-full mod-mod-details-shell"):
                    with ui.column().classes("w-full gap-0 mod-mod-details-header"):
                        ui.label(entry.friendly).classes("text-xl font-black mod-title-small")
                        ui.label(entry.name).classes("mod-subtitle text-sm break-all")
                    with ui.grid(columns=2).classes("mod-detail-grid mod-mod-details-summary"):
                        self._render_mod_detail_item(ui=ui, label="Placement", value=entry.placement.label)
                        self._render_mod_detail_item(ui=ui, label="Type", value=entry.mod_type.label)
                        self._render_mod_detail_item(ui=ui, label="Version", value=version_text)
                        self._render_mod_detail_item(ui=ui, label="Size", value=entry.size_text)
                        self._render_mod_detail_item(ui=ui, label="Origin", value=entry.origin)
                        self._render_mod_detail_item(ui=ui, label="Added", value=entry.added)
                        self._render_mod_detail_item(ui=ui, label="File download", value=download_text)
                        if supports_client_pack:
                            self._render_mod_detail_item(
                                ui=ui,
                                label="Client pack",
                                value=client_pack_text,
                            )
                    if entry.mod_pages:
                        with ui.column().classes(
                            "mod-detail-item mod-mod-page-links mod-mod-details-links gap-1"
                        ):
                            ui.label("Mod pages").classes("mod-stat-label")
                            with ui.row().classes("w-full gap-3 flex-wrap"):
                                for mod_page in mod_pages_in_display_order(entry.mod_pages):
                                    ui.link(
                                        mod_page.name,
                                        mod_page.url,
                                        new_tab=True,
                                    ).props('rel="noopener noreferrer"').classes("mod-mod-page-link")
                    if entry.description is not None:
                        with ui.column().classes("mod-detail-item gap-1 mod-mod-details-description"):
                            ui.label("Description").classes("mod-stat-label")
                            ui.label(entry.description).classes("mod-stat-value break-words")
                    notes = entry.notes
                    if can_edit_notes or notes is not None:
                        with ui.column().classes("mod-detail-item gap-1 mod-mod-details-notes"):
                            ui.label("Notes").classes("mod-stat-label")
                            if can_edit_notes:
                                notes_input = (
                                    ui.textarea(
                                        value=notes or "",
                                        placeholder="Add notes for this mod",
                                    )
                                    .props("filled square dense clearable hide-bottom-space color=accent")
                                    .classes("w-full mod-app-details-field")
                                )
                                with ui.row().classes("w-full justify-end"):
                                    save_notes_button = ui.button("Save Notes", on_click=save_notes).classes(
                                        "mod-list-button"
                                    )
                            else:
                                ui.label(notes or "").classes("mod-stat-value break-words whitespace-pre-wrap")
                    if can_check_update:
                        with ui.column().classes("mod-detail-item gap-2 mod-mod-details-update-check"):
                            ui.label("Updates").classes("mod-stat-label")
                            with ui.row().classes("w-full gap-2 items-center mod-mod-details-update-version-row"):
                                @ui.refreshable
                                def check_update_version_control() -> None:
                                    nonlocal check_update_version_input, check_update_version_select
                                    check_update_version_input = None
                                    check_update_version_select = None
                                    if check_update_versions is None:
                                        check_update_version_input = (
                                            ui.input("Version", placeholder="Latest compatible")
                                            .props(
                                                "filled square dense clearable stack-label "
                                                "hide-bottom-space color=accent"
                                            )
                                            .classes("grow mod-config-input")
                                        )
                                        check_update_version_input.on("update:model-value", reset_update_check_state)
                                        check_update_version_input.on("keydown.enter", check_update)
                                        return
                                    loaded_versions = check_update_versions
                                    version_options = {
                                        "": "Latest compatible",
                                        **{
                                            version.version: mod_portal_version_option_label(version)
                                            for version in loaded_versions.versions
                                        },
                                    }
                                    check_update_version_select = (
                                        ui.select(version_options, value="", label="Version")
                                        .props(
                                            "filled square dense stack-label hide-bottom-space "
                                            "color=accent options-dark"
                                        )
                                        .classes("grow mod-config-input")
                                    )
                                    check_update_version_select.on("update:model-value", reset_update_check_state)

                                check_update_version_control()
                                check_update_versions_button = ui.button(
                                    "Load Versions",
                                    on_click=load_update_versions,
                                ).classes("mod-list-button secondary shrink-0")
                            with ui.row().classes("w-full gap-2 items-center flex-wrap"):
                                check_update_versions_status_label = ui.label("").classes(
                                    "mod-subtitle text-xs grow"
                                )
                            with ui.row().classes("w-full gap-2 items-center flex-wrap"):
                                check_update_button = ui.button(
                                    (
                                        update_check_button_text(cached_update_result)
                                        if available_update_result is not None and cached_update_result is not None
                                        else "Check Update"
                                    ),
                                    on_click=check_update,
                                ).classes("mod-list-button secondary")
                                check_update_status_label = ui.label(
                                    update_check_status_text(cached_update_result)
                                    if cached_update_result is not None
                                    else "Not checked"
                                ).classes(
                                    "mod-subtitle text-sm grow"
                                )
                    if can_edit_properties:
                        with ui.column().classes("mod-detail-item gap-2 mod-mod-details-classification-section"):
                            ui.label("Classification").classes("mod-stat-label")
                            with ui.row().classes(
                                "w-full gap-2 flex-wrap mod-mod-details-classification"
                            ):
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
                                    .classes("mod-app-details-field mod-mod-details-select")
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
                                    .classes("mod-app-details-field mod-mod-details-select")
                                )
                        with ui.column().classes(
                            "w-full gap-3 mod-app-details-section mod-mod-details-editor"
                        ):
                            with ui.column().classes("w-full gap-2 mod-mod-details-subsection"):
                                ui.label("Mod pages").classes("mod-stat-label")
                                ui.label(
                                    "Add links to this mod's project or information pages. "
                                    "Recognized providers are named automatically."
                                ).classes("mod-subtitle text-xs")
                                with ui.column().classes("w-full gap-2") as mod_pages_container:
                                    pass
                                for mod_page in entry.mod_pages:
                                    add_mod_page_row(mod_page)
                                with ui.row().classes(
                                    "w-full gap-2 flex-wrap mod-mod-details-inline-actions"
                                ):
                                    ui.button(
                                        "Add mod page",
                                        on_click=lambda: add_mod_page_row(),
                                    ).classes("mod-list-button secondary")
                            if supports_client_pack:
                                with ui.column().classes("w-full gap-2 mod-mod-details-subsection"):
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
                                        .classes(
                                            "mod-app-details-field mod-mod-details-select mod-client-pack-select"
                                        )
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
                                    previous_client_pack_policy = entry.client_pack.policy

                                    def refresh_client_pack_policy_controls(
                                        *,
                                        apply_transition_default: bool = False,
                                    ) -> None:
                                        nonlocal previous_client_pack_policy
                                        selected_policy = ClientPackPolicy(_value_as_text(client_pack_policy_select))
                                        if apply_transition_default:
                                            client_pack_default_selected_checkbox.set_value(
                                                self._client_pack_default_selected_after_policy_change(
                                                    previous_policy=previous_client_pack_policy,
                                                    selected_policy=selected_policy,
                                                    current_value=bool(
                                                        _value_as_object(
                                                            client_pack_default_selected_checkbox
                                                        )
                                                    ),
                                                )
                                            )
                                        optional_client_pack_controls.set_visibility(
                                            selected_policy is ClientPackPolicy.OPTIONAL
                                        )
                                        alternative_client_pack_controls.set_visibility(
                                            selected_policy is ClientPackPolicy.ALTERNATIVE
                                        )
                                        previous_client_pack_policy = selected_policy

                                    client_pack_policy_select.on(
                                        "update:model-value",
                                        lambda _: refresh_client_pack_policy_controls(
                                            apply_transition_default=True
                                        ),
                                    )
                                    refresh_client_pack_policy_controls()
                            ui.label("Metadata").classes("mod-stat-label mod-mod-details-metadata-label")
                            with ui.row().classes(
                                "w-full gap-2 items-center flex-wrap"
                            ) as metadata_detection_controls:
                                detect_metadata_button = ui.button(
                                    "Detect Metadata",
                                    on_click=lambda: detect_metadata(),
                                ).classes("mod-list-button mod-mod-details-discovery-button")
                                add_provider_context_menu(
                                    button=detect_metadata_button,
                                    action=detect_metadata,
                                )
                                detect_metadata_status_label = ui.label(
                                    "Detect project pages, launcher files, and a classification suggestion in one workflow."
                                ).classes("mod-subtitle text-xs grow")
                            metadata_detection_controls.set_visibility(
                                bool(launcher_metadata_providers)
                            )
                            with ui.row().classes(
                                "w-full gap-2 mod-details-tab-row mod-mod-details-metadata-tabs"
                            ):
                                overrides_button = ui.button(
                                    "Overrides",
                                    on_click=lambda: toggle_metadata_panel("overrides"),
                                ).classes("mod-list-button secondary mod-details-tab-button")
                                launcher_metadata_button = ui.button(
                                    "Launcher Metadata",
                                    on_click=lambda: toggle_metadata_panel("launcher"),
                                ).classes("mod-list-button secondary mod-details-tab-button")
                                launcher_metadata_button.set_visibility(bool(launcher_metadata_providers))
                            with ui.column().classes(
                                "w-full gap-2 mod-mod-details-metadata-panel"
                            ) as overrides_section:
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
                            with ui.column().classes(
                                "w-full gap-2 mod-mod-details-metadata-panel"
                            ) as launcher_metadata_section:
                                ui.label("Launcher Metadata").classes("mod-stat-label")
                                ui.label(launcher_metadata_description).classes("mod-subtitle text-xs")
                                with ui.row().classes("w-full gap-2 flex-wrap"):
                                    find_mod_pages_button = ui.button(
                                        "Find Mod Pages",
                                        on_click=lambda: find_mod_pages_from_local_data(),
                                    ).classes("mod-list-button secondary")
                                    add_provider_context_menu(
                                        button=find_mod_pages_button,
                                        action=find_mod_pages_from_local_data,
                                    )
                                    resolve_launcher_metadata_button = ui.button(
                                        "Resolve from Mod Pages",
                                        on_click=lambda: resolve_launcher_metadata_from_mod_pages(),
                                    ).classes("mod-list-button")
                                    add_provider_context_menu(
                                        button=resolve_launcher_metadata_button,
                                        action=resolve_launcher_metadata_from_mod_pages,
                                    )
                                    fetch_launcher_metadata_button = ui.button(
                                        "Fetch Metadata",
                                        on_click=lambda: fetch_launcher_metadata(),
                                    ).classes("mod-list-button secondary")
                                    add_provider_context_menu(
                                        button=fetch_launcher_metadata_button,
                                        action=fetch_launcher_metadata,
                                    )
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
                                        launcher_url_inputs[provider].on_value_change(
                                            lambda _: ensure_launcher_mod_page_rows(
                                                launcher_urls_from_inputs()
                                            )
                                        )
                                ensure_launcher_mod_page_rows(launcher_urls_from_inputs())
                                metadata_suggestion_label = ui.label(
                                    "Suggested type: fetch metadata to check provider support."
                                ).classes("mod-subtitle text-xs")
                                ui.label(
                                    "Resolve from Mod Pages searches saved and currently edited project links "
                                    "for providers whose file page is blank."
                                ).classes("mod-subtitle text-xs")
                            launcher_metadata_section.set_visibility(False)
                    if available_actions:
                        with ui.column().classes("gap-2 mod-mod-details-danger-zone"):
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
                    with ui.row().classes("w-full justify-end gap-2 mod-mod-details-footer"):
                        ui.button("Close", on_click=dialog.close).classes("mod-list-button secondary")
                        if can_edit_properties:
                            save_properties_button = ui.button(
                                "Save",
                                on_click=save_properties,
                            ).classes("mod-list-button")
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
        show_selection: bool,
        app_friendly: str,
        model: ModWebPageModel,
        user: ModWebUser,
        has_update: bool = False,
    ) -> Checkbox | None:
        row_classes = ["mod-row", "w-full"]
        if not entry.downloadable:
            row_classes.append("blocked")
        elif entry.placement is ModPlacement.SERVER_DISABLED:
            row_classes.append("mod-row-disabled")
        dialog: Dialog | None = None

        def open_mod_info_dialog(_event: object | None = None) -> None:
            nonlocal dialog
            if dialog is None:
                dialog = self._render_mod_info_dialog(
                    ui=ui,
                    entry=entry,
                    model=model,
                    user=user,
                )
            dialog.open()

        row = ui.row().classes(" ".join((*row_classes, "mod-row-clickable")))
        row.on("click", open_mod_info_dialog)
        with row:
            checkbox: Checkbox | None = None
            if show_selection and can_select:
                checkbox = (
                    ui.checkbox(value=False, on_change=on_change)
                    .props("dense")
                    .classes("mod-row-selection-checkbox")
                )
                checkbox.on("click", js_handler="(event) => event.stopPropagation()")
            with ui.column().classes("mod-row-main gap-0"):
                ui.label(entry.friendly).classes("mod-row-title")
                ui.label(entry.name).classes("mod-row-file")
            with ui.row().classes("mod-row-meta"):
                ui.label(entry.size_text).classes("mod-pill size")
                if has_update:
                    ui.label("Update").classes("mod-pill size update")
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
        return checkbox

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
