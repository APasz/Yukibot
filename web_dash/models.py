from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO

from apps.minecraft import (
    Minecraft,
    MinecraftCookingRecipe,
    MinecraftItemRegistrySnapshot,
    MinecraftRecipeBook,
    MinecraftRecipeIngredient,
    MinecraftRecipeItemStack,
    MinecraftRecipeMutation,
    MinecraftRecipeRemoval,
    MinecraftShapedRecipe,
    MinecraftShapelessRecipe,
    MinecraftStonecuttingRecipe,
)
from apps.sevendays import SevenDays, SevenDaysSandboxOptionsSnapshot

from .constants import (
    _REMOTE_NODE_PRESENCE_REQUEST_TIMEOUT,
    _REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
    _REMOTE_NODE_TOKEN_TTL_SECONDS,
    _SAME_ORIGIN_NODE_API_BASE,
    _SAME_ORIGIN_NODE_PROXY_BASE,
    _TITLE_STATS_REFRESH_INTERVAL_SECONDS,
    log,
    traffic_log,
)
from .home import ModWebHomeMixin
from .json_helpers import _json_object, _json_request_object
from .links import mod_web_node_path
from .nicegui_protocols import AsyncRefresh, ModWebUi, RefreshableValue
from .runtime_imports import (
    App,
    App_Manager,
    AppUpdateInfo,
    AppUpdateStatus,
    AuthorityEndpoint,
    AuthorityResource,
    Awaitable,
    BadgeTone,
    BotMetadataModWeb,
    BotMetadataSnapshot,
    Callable,
    Iterable,
    Literal,
    Mapping,
    ModWebUser,
    NodeAccessGrant,
    NodeApiScope,
    NodeAppActivityProviderEntry,
    NodeAppEntry,
    NodeAppResourcePointSummary,
    NodeAppRuntimeSummary,
    NodeAppTransitionState,
    NodeBlueprintList,
    NodeBlueprintMutationResult,
    NodeConfigContent,
    NodeConfigList,
    NodeConsoleActionExecutionResult,
    NodeConsoleActionList,
    NodeConsoleStdoutSnapshot,
    NodeMinecraftRecipeWorkspaceState,
    NodeModList,
    NodeModUploadBatchResult,
    NodeSaveList,
    NodeSaveMutationResult,
    NodeSevenDaysSandboxOptionsState,
    NodeSettingList,
    NodeSettingMutationResult,
    NodeSettingsActionResult,
    NodeSystemSummary,
    Path,
    Power_Level,
    RedirectResponse,
    Request,
    Response,
    Timer,
    Utilities,
    aiohttp,
    app_scope_from_name,
    asyncio,
    cast,
    config,
    issue_node_token,
    json,
    quote,
    read_json_object,
    requests,
    replace,
    tempfile,
    time,
    urlencode,
    urlsplit,
    urlunsplit,
)
from .service_base import ModWebServiceSupport
from .types import (
    ModWebMinecraftItemRegistrySummary,
    ModWebMinecraftRecipeBookSummary,
    ModWebMinecraftRecipeEntry,
    ModWebMinecraftRecipeOperationKind,
    ModWebHomeNodeSummary,
    ModWebNodeAppSection,
    ModWebNodeLink,
    ModWebOverviewPageModel,
    ModWebPageModel,
    ModWebPageLoadWarning,
    ModWebSevenDaysSandboxOptionEntry,
    ModWebSevenDaysSandboxOptionsSummary,
    ModWebTitleStat,
    ModWebTitleStatLine,
)
from .utils import _http_exception, _is_executor_shutdown_error

if TYPE_CHECKING:
    from nicegui.elements.upload_files import FileUpload


@dataclass(frozen=True, slots=True)
class _LocalAppPageData:
    app: App
    app_entry: NodeAppEntry
    configs: NodeConfigList
    saves: NodeSaveList | None
    blueprints: NodeBlueprintList | None
    settings: NodeSettingList | None
    console_actions: NodeConsoleActionList | None
    app_start_blocked: bool
    mods: NodeModList | None
    app_stats: NodeAppRuntimeSummary | None
    minecraft_recipes: ModWebMinecraftRecipeBookSummary | None = None
    minecraft_item_registry: ModWebMinecraftItemRegistrySummary | None = None
    sevendays_sandbox_options: ModWebSevenDaysSandboxOptionsSummary | None = None
    load_warnings: tuple[ModWebPageLoadWarning, ...] = ()


class ModWebModelsMixin(ModWebServiceSupport):
    _PORTAL_OWNED_PATH_PREFIXES: tuple[str, ...] = (
        "/auth/",
        "/mod-web/assets/",
        "/mod-web/dev/",
    )
    _NODE_SCOPED_PATH_PREFIX: str = "/mod-web/nodes/"
    _DEFAULT_APP_COLOR_HEX: str = "#96212B"
    _APP_COLOR_HEX_BY_SCOPE: dict[str, str] = {
        "base": "#6B7280",
        "beammp": "#F97316",
        "ets": "#2563EB",
        "factorio": "#DC6B0F",
        "minecraft": "#22C55E",
        "satisfactory": "#F59E0B",
        "sevendays": "#B91C1C",
    }

    @staticmethod
    def _dev_cluster_node_links() -> tuple[ModWebNodeLink, ...]:
        raw_payload = config.env_opt("DEV_CLUSTER_NODE_LINKS_JSON")
        if raw_payload is None:
            return ()
        try:
            payload = json.loads(raw_payload)
        except ValueError as xcp:
            log.warning("Mod web failed to parse DEV_CLUSTER_NODE_LINKS_JSON: %s", xcp)
            return ()
        if not isinstance(payload, list):
            log.warning("Mod web ignored DEV_CLUSTER_NODE_LINKS_JSON because the payload is not a JSON array.")
            return ()

        links: list[ModWebNodeLink] = []
        for item in payload:
            if not isinstance(item, dict):
                log.warning("Mod web ignored a DEV_CLUSTER_NODE_LINKS_JSON entry because it is not a JSON object.")
                continue
            try:
                node_name = str(item["node_name"]).strip()
                label = str(item.get("label", node_name)).strip()
                node_api_public_base_url = str(item["node_api_public_base_url"]).strip()
                if not node_name or not label or not node_api_public_base_url:
                    raise ValueError("fields must not be blank")
                resolved_public_base_url = config.resolve_node_api_public_base_url(
                    node_api_public_base_url,
                    mod_web_public_base_url=config.MOD_WEB_SERVER.public_base_url,
                )
                node_api_base_url = config.resolve_node_api_base_url(
                    resolved_public_base_url,
                    source_name="DEV_CLUSTER_NODE_LINKS_JSON",
                )
            except (KeyError, TypeError, ValueError) as xcp:
                log.warning("Mod web ignored invalid DEV_CLUSTER_NODE_LINKS_JSON entry: %s", xcp)
                continue
            links.append(
                ModWebNodeLink(
                    node_name=node_name,
                    label=label,
                    url=mod_web_node_path(node_name),
                    api_base_url=node_api_base_url,
                    api_url=f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{quote(node_name, safe='')}/apps",
                    is_current=False,
                    latency_probe_url=f"{node_api_base_url.rstrip('/')}/ping",
                    presence_stream_url=f"{node_api_base_url.rstrip('/')}/presence/stream",
                )
            )
        return tuple(links)

    @staticmethod
    def _portal_default_node_snapshot() -> config.BotMetadataSnapshot | None:
        snapshots = ModWebModelsMixin._known_bot_snapshots()
        for snapshot in snapshots:
            if snapshot.profile.bot_profile is config.BotProfileName.YUKI and snapshot.features.mod_web is not None:
                return snapshot
        for snapshot in snapshots:
            if snapshot.features.mod_web is not None:
                return snapshot
        return None

    @classmethod
    def _portal_default_node_name(cls) -> str | None:
        dev_cluster_links = cls._dev_cluster_node_links()
        for link in dev_cluster_links:
            if link.node_name.casefold() == config.BotProfileName.YUKI.value.casefold():
                return link.node_name
        if dev_cluster_links:
            return dev_cluster_links[0].node_name
        snapshot = cls._portal_default_node_snapshot()
        mod_web = None if snapshot is None else snapshot.features.mod_web
        if mod_web is None:
            return None
        return mod_web.node_name

    @classmethod
    def _default_mod_web_node_name(cls) -> str:
        return cls._portal_default_node_name() or config.MOD_WEB_SERVER.node_name

    @staticmethod
    def _absolute_node_api_base_url(api_base_url: str) -> str:
        parsed = urlsplit(api_base_url)
        if parsed.scheme and parsed.netloc:
            return api_base_url.rstrip("/")
        if api_base_url.startswith("/"):
            return f"{config.MOD_WEB_SERVER.public_base_url.rstrip('/')}{api_base_url.rstrip('/')}"
        raise RuntimeError(f"Node API base URL must be absolute or root-relative, got {api_base_url!r}.")

    @classmethod
    def _resolved_app_color_hex(
        cls,
        *,
        app_name: str,
        scope: str | None,
        color_hex: str | None,
    ) -> str | None:
        normalised_color_hex: str | None = None if color_hex is None else color_hex.strip() or None
        if normalised_color_hex is not None and normalised_color_hex.casefold() != cls._DEFAULT_APP_COLOR_HEX.casefold():
            return normalised_color_hex
        resolved_scope: str | None = None
        if isinstance(scope, str) and scope.strip():
            resolved_scope = scope.strip().casefold()
        else:
            resolved_scope = app_scope_from_name(app_name)
            if resolved_scope is not None:
                resolved_scope = resolved_scope.casefold()
        if resolved_scope is None:
            return normalised_color_hex
        return cls._APP_COLOR_HEX_BY_SCOPE.get(resolved_scope, normalised_color_hex)

    @classmethod
    def _resolved_remote_app_entry(cls, entry: NodeAppEntry) -> NodeAppEntry:
        resolved_color_hex = cls._resolved_app_color_hex(
            app_name=entry.name,
            scope=entry.scope,
            color_hex=entry.color_hex,
        )
        if resolved_color_hex == entry.color_hex:
            return entry
        return replace(entry, color_hex=resolved_color_hex)

    def _current_node_link(self) -> ModWebNodeLink:
        node_name = config.MOD_WEB_SERVER.node_name
        api_base_url = self._absolute_node_api_base_url(config.MOD_WEB_SERVER.node_api_base_url)
        return ModWebNodeLink(
            node_name=node_name,
            label=self._current_node_label(),
            url=mod_web_node_path(node_name),
            api_base_url=api_base_url,
            api_url=f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{quote(node_name, safe='')}/apps",
            is_current=True,
            latency_probe_url=self._node_api.ping_url(base_url=api_base_url),
            presence_stream_url=self._node_api.presence_stream_url(base_url=api_base_url),
        )

    @staticmethod
    def _node_app_api_url(node: ModWebNodeLink, app_name: str) -> str:
        return f"{node.api_url.rstrip('/')}/{quote(app_name, safe='')}"

    @staticmethod
    def _page_load_warning(*, section_label: str, error: BaseException) -> ModWebPageLoadWarning:
        detail: str = str(error).strip() or type(error).__name__
        return ModWebPageLoadWarning(
            title=f"{section_label} unavailable",
            detail=detail,
        )

    def _warn_page_section_load_failure(
        self,
        *,
        context: str,
        section_label: str,
        error: BaseException,
        load_warnings: list[ModWebPageLoadWarning],
    ) -> None:
        if not (self._shutting_down or config.IS_SHUTTINGDOWN):
            log.warning("%s section load failed: %s", context, error)
        load_warnings.append(self._page_load_warning(section_label=section_label, error=error))

    @staticmethod
    def _minecraft_recipe_item_text(item_stack: MinecraftRecipeItemStack) -> str:
        return item_stack.kubejs_value

    @staticmethod
    def _minecraft_recipe_ingredient_text(ingredient: MinecraftRecipeIngredient) -> str:
        return ingredient.kubejs_value

    @classmethod
    def _minecraft_recipe_entry(cls, mutation: MinecraftRecipeMutation) -> ModWebMinecraftRecipeEntry:
        if isinstance(mutation, MinecraftRecipeRemoval):
            filter_payload = mutation.filter.kubejs_payload
            if mutation.filter.recipe_id is not None:
                title = mutation.filter.recipe_id
            elif mutation.filter.output is not None:
                title = f"Output {cls._minecraft_recipe_ingredient_text(mutation.filter.output)}"
            elif mutation.filter.input is not None:
                title = f"Input {cls._minecraft_recipe_ingredient_text(mutation.filter.input)}"
            elif mutation.filter.mod_id is not None:
                title = f"Mod {mutation.filter.mod_id}"
            else:
                title = "Recipe filter"
            detail = ", ".join(f"{key}: {value}" for key, value in filter_payload.items())
            return ModWebMinecraftRecipeEntry(
                operation=ModWebMinecraftRecipeOperationKind.REMOVE,
                kind_label="Remove",
                title=title,
                detail=detail,
                recipe_id=mutation.filter.recipe_id,
            )
        if isinstance(mutation, MinecraftShapelessRecipe):
            ingredients = ", ".join(cls._minecraft_recipe_ingredient_text(item) for item in mutation.ingredients)
            return ModWebMinecraftRecipeEntry(
                operation=ModWebMinecraftRecipeOperationKind.ADD,
                kind_label="Shapeless",
                title=cls._minecraft_recipe_item_text(mutation.output),
                detail=f"Ingredients: {ingredients}",
                recipe_id=mutation.recipe_id,
            )
        if isinstance(mutation, MinecraftShapedRecipe):
            pattern = " / ".join(mutation.pattern)
            key_text = ", ".join(
                f"{symbol}: {cls._minecraft_recipe_ingredient_text(ingredient)}"
                for symbol, ingredient in sorted(mutation.key.items())
            )
            return ModWebMinecraftRecipeEntry(
                operation=ModWebMinecraftRecipeOperationKind.ADD,
                kind_label="Shaped",
                title=cls._minecraft_recipe_item_text(mutation.output),
                detail=f"Pattern: {pattern}; Key: {key_text}",
                recipe_id=mutation.recipe_id,
            )
        if isinstance(mutation, MinecraftCookingRecipe):
            detail = f"Input: {cls._minecraft_recipe_ingredient_text(mutation.ingredient)}"
            extras: list[str] = []
            if mutation.experience is not None:
                extras.append(f"XP {mutation.experience:g}")
            if mutation.cooking_time_ticks is not None:
                extras.append(f"{mutation.cooking_time_ticks} ticks")
            if extras:
                detail = f"{detail}; {', '.join(extras)}"
            return ModWebMinecraftRecipeEntry(
                operation=ModWebMinecraftRecipeOperationKind.ADD,
                kind_label=mutation.kind.value.replace("_", " ").title(),
                title=cls._minecraft_recipe_item_text(mutation.output),
                detail=detail,
                recipe_id=mutation.recipe_id,
            )
        if isinstance(mutation, MinecraftStonecuttingRecipe):
            return ModWebMinecraftRecipeEntry(
                operation=ModWebMinecraftRecipeOperationKind.ADD,
                kind_label="Stonecutting",
                title=cls._minecraft_recipe_item_text(mutation.output),
                detail=f"Input: {cls._minecraft_recipe_ingredient_text(mutation.ingredient)}",
                recipe_id=mutation.recipe_id,
            )
        raise TypeError(f"Unsupported Minecraft recipe mutation: {type(mutation).__name__}")

    def _minecraft_recipe_summary(self, app: App) -> ModWebMinecraftRecipeBookSummary | None:
        if not isinstance(app, Minecraft):
            return None
        data_path = ".yukibot/recipes.json"
        script_path = "kubejs/server_scripts/yuki_recipes.js"
        try:
            recipe_book: MinecraftRecipeBook = app.load_kubejs_recipe_book()
        except Exception as xcp:
            return ModWebMinecraftRecipeBookSummary(
                data_path=data_path,
                script_path=script_path,
                load_error=str(xcp) or type(xcp).__name__,
            )
        return ModWebMinecraftRecipeBookSummary(
            data_path=data_path,
            script_path=script_path,
            entries=tuple(self._minecraft_recipe_entry(mutation) for mutation in recipe_book.mutations),
            mutation_mappings=tuple(mutation.to_mapping() for mutation in recipe_book.mutations),
        )

    def _minecraft_item_registry_summary(self, app: App) -> ModWebMinecraftItemRegistrySummary | None:
        if not isinstance(app, Minecraft):
            return None
        data_path = ".yukibot/registries/items.json"
        item_registry_path_exists = app._resolve_existing_yukibot_data_path(
            current_path=app._yukibot_item_registry_path(),
            legacy_path=app._legacy_yukibot_item_registry_path(),
        ).exists()
        try:
            item_registry: MinecraftItemRegistrySnapshot = app.load_kubejs_item_registry()
        except Exception as xcp:
            return ModWebMinecraftItemRegistrySummary(
                data_path=data_path,
                file_exists=item_registry_path_exists,
                load_error=str(xcp) or type(xcp).__name__,
            )
        return ModWebMinecraftItemRegistrySummary(
            data_path=data_path,
            item_ids=item_registry.item_ids,
            file_exists=item_registry_path_exists,
            generated_at_epoch_ms=item_registry.generated_at_epoch_ms,
        )

    @staticmethod
    def _sevendays_sandbox_options_summary_from_snapshot(
        *,
        data_path: str,
        file_exists: bool,
        snapshot: SevenDaysSandboxOptionsSnapshot,
    ) -> ModWebSevenDaysSandboxOptionsSummary:
        return ModWebSevenDaysSandboxOptionsSummary(
            data_path=data_path,
            file_exists=file_exists,
            generated_at=snapshot.generated_at,
            sandbox_code=snapshot.sandbox_code,
            app_version=snapshot.app_version,
            options=tuple(
                ModWebSevenDaysSandboxOptionEntry(
                    section=option.section,
                    key=option.key,
                    value_index=option.value_index,
                    value_label=option.value_label,
                    default_index=option.default_index,
                    default_label=option.default_label,
                )
                for option in snapshot.options
            ),
        )

    def _sevendays_sandbox_options_summary(self, app: App) -> ModWebSevenDaysSandboxOptionsSummary | None:
        if not isinstance(app, SevenDays) or not app.supports_sevendays_sandbox_options:
            return None
        data_path = ".yukibot/sandbox_options.json"
        if not app.sandbox_options_file_exists:
            return ModWebSevenDaysSandboxOptionsSummary(
                data_path=data_path,
                file_exists=False,
                app_version=None if app.cfg.version is None else app.cfg.version.display_value,
            )
        try:
            snapshot = app.load_sandbox_options_snapshot()
        except Exception as xcp:
            return ModWebSevenDaysSandboxOptionsSummary(
                data_path=data_path,
                file_exists=app.sandbox_options_file_exists,
                load_error=str(xcp) or type(xcp).__name__,
            )
        return self._sevendays_sandbox_options_summary_from_snapshot(
            data_path=data_path,
            file_exists=app.sandbox_options_file_exists,
            snapshot=snapshot,
        )

    async def _build_local_app_page_data(self, app: App, *, user: ModWebUser) -> _LocalAppPageData:
        can_manage_app: bool = self._user_has_level(user, Power_Level.user)
        app_entry: NodeAppEntry = self._node_api.build_app_entry(app)
        supports_configs: bool = app_entry.supports_configs
        config_read_level: Power_Level = app_entry.config_read_level
        load_warnings: list[ModWebPageLoadWarning] = []
        empty_configs = self._empty_config_list(
            app_name=app_entry.name,
            app_friendly=app_entry.friendly,
            node_name=app_entry.node,
        )
        context: str = f"Local mod web app page: app={app_entry.name}"
        if supports_configs and self._user_has_level(user, config_read_level):
            try:
                configs = self._node_api.build_config_list(app, actor_user_id=user.discord_id)
            except Exception as xcp:
                self._warn_page_section_load_failure(
                    context=context,
                    section_label="Configs",
                    error=xcp,
                    load_warnings=load_warnings,
                )
                configs = empty_configs
        else:
            configs = empty_configs
        if app_entry.supports_saves and can_manage_app:
            try:
                saves = await self._node_api.build_save_list(app)
            except Exception as xcp:
                self._warn_page_section_load_failure(
                    context=context,
                    section_label="Saves",
                    error=xcp,
                    load_warnings=load_warnings,
                )
                saves = None
        else:
            saves = None
        if app_entry.supports_blueprints and can_manage_app:
            try:
                blueprints = self._node_api.build_blueprint_list(app, actor_user_id=user.discord_id)
            except Exception as xcp:
                self._warn_page_section_load_failure(
                    context=context,
                    section_label="Blueprints",
                    error=xcp,
                    load_warnings=load_warnings,
                )
                blueprints = None
        else:
            blueprints = None
        if app_entry.supports_settings and can_manage_app:
            try:
                settings = self._node_api.build_setting_list(app=app, actor_user_id=user.discord_id)
            except Exception as xcp:
                self._warn_page_section_load_failure(
                    context=context,
                    section_label="Settings",
                    error=xcp,
                    load_warnings=load_warnings,
                )
                settings = None
        else:
            settings = None
        if app_entry.supports_console_actions and can_manage_app:
            try:
                console_actions = self._node_api.build_console_action_list(app=app, actor_user_id=user.discord_id)
            except Exception as xcp:
                self._warn_page_section_load_failure(
                    context=context,
                    section_label="Console",
                    error=xcp,
                    load_warnings=load_warnings,
                )
                console_actions = None
        else:
            console_actions = None
        app_start_blocked: bool = self._app_start_blocked_local(app)
        if app_entry.supports_mods:
            mods: NodeModList | None = await self._node_api.build_mod_list(app)
            app_stats: NodeAppRuntimeSummary | None = mods.app_stats
        else:
            mods = None
            app_stats = await self._node_api.build_app_runtime_summary(app)
        minecraft_recipes = self._minecraft_recipe_summary(app)
        minecraft_item_registry = self._minecraft_item_registry_summary(app)
        sevendays_sandbox_options = self._sevendays_sandbox_options_summary(app)
        return _LocalAppPageData(
            app=app,
            app_entry=app_entry,
            configs=configs,
            saves=saves,
            blueprints=blueprints,
            settings=settings,
            console_actions=console_actions,
            app_start_blocked=app_start_blocked,
            mods=mods,
            app_stats=app_stats,
            minecraft_recipes=minecraft_recipes,
            minecraft_item_registry=minecraft_item_registry,
            sevendays_sandbox_options=sevendays_sandbox_options,
            load_warnings=tuple(load_warnings),
        )

    def _page_model_from_local_page_data(
        self,
        page_data: _LocalAppPageData,
        *,
        can_manage_app: bool,
    ) -> ModWebPageModel:
        if page_data.mods is None:
            raise ValueError(f"Local app page data for {page_data.app_entry.name!r} does not include mods.")
        current_node: ModWebNodeLink = self._current_node_link()
        mods: NodeModList = page_data.mods
        traffic_log.info(
            "Mod web page model built: app=%s mods=%s configs=%s saves=%s blueprints=%s settings=%s",
            page_data.app.name,
            mods.summary.total_count,
            len(page_data.configs.configs),
            len(page_data.saves.saves) if page_data.saves is not None else 0,
            len(page_data.blueprints.blueprints) if page_data.blueprints is not None else 0,
            len(page_data.settings.settings) if page_data.settings is not None else 0,
        )
        return self._remote_page_model(
            node=current_node,
            app_scope=page_data.app_entry.scope,
            mods=mods,
            supports_configs=page_data.app_entry.supports_configs,
            config_read_level=page_data.app_entry.config_read_level,
            config_write_level=page_data.app_entry.config_write_level,
            supports_save_uploads=page_data.app_entry.supports_save_uploads,
            supports_save_rename=page_data.app_entry.supports_save_rename,
            save_write_level=page_data.app_entry.save_write_level,
            configs=page_data.configs,
            saves=page_data.saves,
            blueprints=page_data.blueprints,
            settings=page_data.settings,
            console_actions=page_data.console_actions,
            map_url=page_data.app_entry.map_url,
            can_write_map_annotations=page_data.app_entry.map_url is not None and can_manage_app,
            supports_chat=page_data.app_entry.supports_chat,
            supports_updates=page_data.app_entry.supports_updates,
            chat_url=(self.app_chat_path(page_data.app_entry.name) if page_data.app_entry.supports_chat else None),
            update_info=page_data.app_entry.update_info,
            update_status=page_data.app_entry.update_status,
            app_start_blocked=page_data.app_start_blocked,
            app_color_hex=page_data.app_entry.color_hex,
            resource_points=page_data.app_entry.resource_points,
            app_title_font_preset=page_data.app_entry.title_font_preset,
            app_notes=page_data.app_entry.notes,
            lifecycle_notice_started=page_data.app_entry.lifecycle_notice_started,
            lifecycle_notice_stopped=page_data.app_entry.lifecycle_notice_stopped,
            lifecycle_notice_crashed=page_data.app_entry.lifecycle_notice_crashed,
            relay_notice_player_session=page_data.app_entry.relay_notice_player_session,
            relay_notice_player_death=page_data.app_entry.relay_notice_player_death,
            relay_notice_progress=page_data.app_entry.relay_notice_progress,
            relay_notice_progress_label=page_data.app_entry.relay_notice_progress_label,
            relay_advancements_enabled=page_data.app_entry.relay_advancements_enabled,
            relay_advancement_term=page_data.app_entry.relay_advancement_term,
            activity_providers=page_data.app_entry.activity_providers,
            load_warnings=page_data.load_warnings,
            minecraft_recipes=page_data.minecraft_recipes,
            minecraft_item_registry=page_data.minecraft_item_registry,
            sevendays_sandbox_options=page_data.sevendays_sandbox_options,
        )

    def _overview_model_from_local_page_data(
        self, page_data: _LocalAppPageData, *, can_manage_app: bool
    ) -> ModWebOverviewPageModel:
        current_node: ModWebNodeLink = self._current_node_link()
        traffic_log.info(
            "Mod web overview model built: app=%s configs=%s saves=%s blueprints=%s settings=%s",
            page_data.app.name,
            len(page_data.configs.configs),
            len(page_data.saves.saves) if page_data.saves is not None else 0,
            len(page_data.blueprints.blueprints) if page_data.blueprints is not None else 0,
            len(page_data.settings.settings) if page_data.settings is not None else 0,
        )
        return self._remote_overview_page_model(
            node=current_node,
            app_name=page_data.app_entry.name,
            app_friendly=page_data.app_entry.friendly,
            app_scope=page_data.app_entry.scope,
            app_color_hex=page_data.app_entry.color_hex,
            supports_configs=page_data.app_entry.supports_configs,
            config_read_level=page_data.app_entry.config_read_level,
            config_write_level=page_data.app_entry.config_write_level,
            supports_save_uploads=page_data.app_entry.supports_save_uploads,
            supports_save_rename=page_data.app_entry.supports_save_rename,
            save_write_level=page_data.app_entry.save_write_level,
            configs=page_data.configs,
            saves=page_data.saves,
            blueprints=page_data.blueprints,
            settings=page_data.settings,
            console_actions=page_data.console_actions,
            map_url=page_data.app_entry.map_url,
            can_write_map_annotations=page_data.app_entry.map_url is not None and can_manage_app,
            supports_chat=page_data.app_entry.supports_chat,
            supports_updates=page_data.app_entry.supports_updates,
            chat_url=(self.app_chat_path(page_data.app_entry.name) if page_data.app_entry.supports_chat else None),
            update_info=page_data.app_entry.update_info,
            update_status=page_data.app_entry.update_status,
            app_stats=page_data.app_stats,
            app_start_blocked=page_data.app_start_blocked,
            resource_points=page_data.app_entry.resource_points,
            app_title_font_preset=page_data.app_entry.title_font_preset,
            app_notes=page_data.app_entry.notes,
            lifecycle_notice_started=page_data.app_entry.lifecycle_notice_started,
            lifecycle_notice_stopped=page_data.app_entry.lifecycle_notice_stopped,
            lifecycle_notice_crashed=page_data.app_entry.lifecycle_notice_crashed,
            relay_notice_player_session=page_data.app_entry.relay_notice_player_session,
            relay_notice_player_death=page_data.app_entry.relay_notice_player_death,
            relay_notice_progress=page_data.app_entry.relay_notice_progress,
            relay_notice_progress_label=page_data.app_entry.relay_notice_progress_label,
            relay_advancements_enabled=page_data.app_entry.relay_advancements_enabled,
            relay_advancement_term=page_data.app_entry.relay_advancement_term,
            activity_providers=page_data.app_entry.activity_providers,
            load_warnings=page_data.load_warnings,
        )

    async def _build_page_model(self, app: App, *, user: ModWebUser) -> ModWebPageModel:
        page_data = await self._build_local_app_page_data(app, user=user)
        return self._page_model_from_local_page_data(
            page_data,
            can_manage_app=self._user_has_level(user, Power_Level.user),
        )

    async def _build_overview_page_model(self, app: App, *, user: ModWebUser) -> ModWebOverviewPageModel:
        page_data = await self._build_local_app_page_data(app, user=user)
        return self._overview_model_from_local_page_data(
            page_data,
            can_manage_app=self._user_has_level(user, Power_Level.user),
        )

    def _remote_page_model(
        self,
        *,
        node: ModWebNodeLink,
        app_scope: str | None,
        mods: NodeModList,
        supports_configs: bool,
        config_read_level: Power_Level,
        config_write_level: Power_Level,
        supports_save_uploads: bool,
        supports_save_rename: bool,
        save_write_level: Power_Level,
        configs: NodeConfigList,
        saves: NodeSaveList | None,
        blueprints: NodeBlueprintList | None,
        settings: NodeSettingList | None,
        console_actions: NodeConsoleActionList | None,
        map_url: str | None,
        can_write_map_annotations: bool,
        supports_chat: bool,
        supports_updates: bool,
        chat_url: str | None,
        update_info: AppUpdateInfo | None,
        update_status: AppUpdateStatus | None,
        app_start_blocked: bool,
        app_color_hex: str | None,
        resource_points: NodeAppResourcePointSummary | None,
        app_title_font_preset: str,
        app_notes: str | None,
        lifecycle_notice_started: bool,
        lifecycle_notice_stopped: bool,
        lifecycle_notice_crashed: bool,
        relay_notice_player_session: bool | None = None,
        relay_notice_player_death: bool | None = None,
        relay_notice_progress: bool | None = None,
        relay_notice_progress_label: str | None = None,
        relay_advancements_enabled: bool | None = None,
        relay_advancement_term: str | None = None,
        activity_providers: tuple[NodeAppActivityProviderEntry, ...] = (),
        load_warnings: tuple[ModWebPageLoadWarning, ...] = (),
        minecraft_recipes: ModWebMinecraftRecipeBookSummary | None = None,
        minecraft_item_registry: ModWebMinecraftItemRegistrySummary | None = None,
        sevendays_sandbox_options: ModWebSevenDaysSandboxOptionsSummary | None = None,
    ) -> ModWebPageModel:
        app_api_url: str = self._node_app_api_url(node, mods.app_name)
        return cast(
            ModWebPageModel,
            self._page_model_with_tabs(
                ModWebPageModel(
                    node_name=node.node_name,
                    app_name=mods.app_name,
                    app_friendly=mods.app_friendly,
                    app_color_hex=app_color_hex,
                    app_scope=app_scope,
                    supports_configs=supports_configs,
                    config_read_level=config_read_level,
                    config_write_level=config_write_level,
                    supports_save_uploads=supports_save_uploads,
                    supports_save_rename=supports_save_rename,
                    save_write_level=save_write_level,
                    mods=mods,
                    configs=configs,
                    saves=saves,
                    app_stats=mods.app_stats,
                    app_start_blocked=app_start_blocked,
                    settings=settings,
                    app_title_font_preset=app_title_font_preset,
                    console_actions=console_actions,
                    blueprints=blueprints,
                    map_url=map_url,
                    can_write_map_annotations=map_url is not None and can_write_map_annotations,
                    supports_chat=supports_chat,
                    supports_updates=supports_updates,
                    chat_url=chat_url,
                    update_info=update_info,
                    update_status=update_status,
                    resource_points=resource_points,
                    app_notes=app_notes,
                    lifecycle_notice_started=lifecycle_notice_started,
                    lifecycle_notice_stopped=lifecycle_notice_stopped,
                    lifecycle_notice_crashed=lifecycle_notice_crashed,
                    relay_notice_player_session=relay_notice_player_session,
                    relay_notice_player_death=relay_notice_player_death,
                    relay_notice_progress=relay_notice_progress,
                    relay_notice_progress_label=relay_notice_progress_label,
                    relay_advancements_enabled=relay_advancements_enabled,
                    relay_advancement_term=relay_advancement_term,
                    activity_providers=activity_providers,
                    load_warnings=load_warnings,
                    minecraft_recipes=minecraft_recipes,
                    minecraft_item_registry=minecraft_item_registry,
                    sevendays_sandbox_options=sevendays_sandbox_options,
                    download_all_url=f"{app_api_url}/mods/download?{urlencode({'enabled_only': 'false'})}",
                    download_enabled_url=f"{app_api_url}/mods/download?{urlencode({'enabled_only': 'true'})}",
                    mod_download_urls={
                        mod.name: f"{app_api_url}/mods/{quote(mod.name, safe='')}/download"
                        for mod in mods.mods
                        if mod.downloadable
                    },
                    map_api_url=f"{app_api_url}/map" if map_url is not None else None,
                    minecraft_item_icon_api_url=f"{app_api_url}/minecraft/recipes/item-icon",
                ),
            ),
        )

    def _remote_overview_page_model(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str,
        app_friendly: str,
        app_scope: str | None,
        app_color_hex: str | None,
        supports_configs: bool,
        config_read_level: Power_Level,
        config_write_level: Power_Level,
        supports_save_uploads: bool,
        supports_save_rename: bool,
        save_write_level: Power_Level,
        configs: NodeConfigList,
        saves: NodeSaveList | None,
        blueprints: NodeBlueprintList | None,
        settings: NodeSettingList | None,
        console_actions: NodeConsoleActionList | None,
        map_url: str | None,
        can_write_map_annotations: bool,
        supports_chat: bool,
        supports_updates: bool,
        chat_url: str | None,
        update_info: AppUpdateInfo | None,
        update_status: AppUpdateStatus | None,
        app_stats: NodeAppRuntimeSummary | None,
        app_start_blocked: bool,
        resource_points: NodeAppResourcePointSummary | None,
        app_title_font_preset: str,
        app_notes: str | None,
        lifecycle_notice_started: bool,
        lifecycle_notice_stopped: bool,
        lifecycle_notice_crashed: bool,
        relay_notice_player_session: bool | None = None,
        relay_notice_player_death: bool | None = None,
        relay_notice_progress: bool | None = None,
        relay_notice_progress_label: str | None = None,
        relay_advancements_enabled: bool | None = None,
        relay_advancement_term: str | None = None,
        activity_providers: tuple[NodeAppActivityProviderEntry, ...] = (),
        load_warnings: tuple[ModWebPageLoadWarning, ...] = (),
    ) -> ModWebOverviewPageModel:
        app_api_url: str = self._node_app_api_url(node, app_name)
        return cast(
            ModWebOverviewPageModel,
            self._page_model_with_tabs(
                ModWebOverviewPageModel(
                    node_name=node.node_name,
                    app_name=app_name,
                    app_friendly=app_friendly,
                    app_color_hex=app_color_hex,
                    app_scope=app_scope,
                    supports_configs=supports_configs,
                    config_read_level=config_read_level,
                    config_write_level=config_write_level,
                    supports_save_uploads=supports_save_uploads,
                    supports_save_rename=supports_save_rename,
                    save_write_level=save_write_level,
                    configs=configs,
                    saves=saves,
                    app_stats=app_stats,
                    app_start_blocked=app_start_blocked,
                    settings=settings,
                    app_title_font_preset=app_title_font_preset,
                    console_actions=console_actions,
                    blueprints=blueprints,
                    map_url=map_url,
                    map_api_url=f"{app_api_url}/map" if map_url is not None else None,
                    minecraft_item_icon_api_url=f"{app_api_url}/minecraft/recipes/item-icon",
                    can_write_map_annotations=map_url is not None and can_write_map_annotations,
                    supports_chat=supports_chat,
                    supports_updates=supports_updates,
                    chat_url=chat_url,
                    update_info=update_info,
                    update_status=update_status,
                    resource_points=resource_points,
                    app_notes=app_notes,
                    lifecycle_notice_started=lifecycle_notice_started,
                    lifecycle_notice_stopped=lifecycle_notice_stopped,
                    lifecycle_notice_crashed=lifecycle_notice_crashed,
                    relay_notice_player_session=relay_notice_player_session,
                    relay_notice_player_death=relay_notice_player_death,
                    relay_notice_progress=relay_notice_progress,
                    relay_notice_progress_label=relay_notice_progress_label,
                    relay_advancements_enabled=relay_advancements_enabled,
                    relay_advancement_term=relay_advancement_term,
                    activity_providers=activity_providers,
                    load_warnings=load_warnings,
                ),
            ),
        )

    def _node_links(self) -> tuple[ModWebNodeLink, ...]:
        links: dict[str, ModWebNodeLink] = {}
        if config.ACTIVE_BOT_PROFILE.name is not config.BotProfileName.PORTAL:
            current = self._current_node_link()
            links[current.node_name.casefold()] = current

        for node in self._dev_cluster_node_links():
            key = node.node_name.casefold()
            if key in links:
                continue
            links[key] = node

        for snapshot in self._known_bot_snapshots():
            mod_web: BotMetadataModWeb | None = snapshot.features.mod_web
            if mod_web is None:
                continue
            node_name: str = mod_web.node_name
            key: str = node_name.casefold()
            if key in links:
                continue
            links[key] = ModWebNodeLink(
                node_name=node_name,
                label=snapshot.profile.label or node_name,
                url=f"/mod-web/nodes/{quote(node_name, safe='')}",
                api_base_url=mod_web.node_api_base_url.rstrip("/"),
                api_url=f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{quote(node_name, safe='')}/apps",
                is_current=False,
                latency_probe_url=self._node_api.ping_url(base_url=mod_web.node_api_base_url),
                presence_stream_url=self._node_api.presence_stream_url(base_url=mod_web.node_api_base_url),
            )
        return tuple(links.values())

    def _current_node_label(self) -> str:
        node_name: str = config.MOD_WEB_SERVER.node_name
        key: str = node_name.casefold()
        for snapshot in self._known_bot_snapshots():
            mod_web: BotMetadataModWeb | None = snapshot.features.mod_web
            if mod_web is None or mod_web.node_name.casefold() != key:
                continue
            if snapshot.profile.label:
                return snapshot.profile.label
        if config.ACTIVE_BOT_PROFILE.name is config.BotProfileName.PORTAL:
            return "Portal"
        if key == config.ACTIVE_BOT_PROFILE.name.value.casefold():
            return config.ACTIVE_BOT_PROFILE.name.value.title()
        return node_name

    def _known_bot_snapshot_for_node(self, *, node_name: str) -> config.BotMetadataSnapshot | None:
        key: str = node_name.casefold()
        for snapshot in self._known_bot_snapshots():
            mod_web: BotMetadataModWeb | None = snapshot.features.mod_web
            if mod_web is None or mod_web.node_name.casefold() != key:
                continue
            return snapshot
        return None

    def _remote_node_link(self, node_name: str) -> ModWebNodeLink:
        key: str = node_name.casefold()
        for node in self._node_links():
            if node.node_name.casefold() == key:
                return node
        raise _http_exception(404, f"Unknown node: {node_name}")

    def _remote_apps(self, node: ModWebNodeLink, user: ModWebUser) -> tuple[NodeAppEntry, ...]:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=None,
            path="/apps",
            scopes=(NodeApiScope.APPS_READ,),
            user=user,
            timeout=_REMOTE_NODE_PRESENCE_REQUEST_TIMEOUT,
        )
        return self._remote_apps_from_payload(payload)

    async def _remote_apps_async(self, node: ModWebNodeLink, user: ModWebUser) -> tuple[NodeAppEntry, ...]:
        payload: dict[str, object] = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/apps",
            scopes=(NodeApiScope.APPS_READ,),
            user=user,
            timeout=_REMOTE_NODE_PRESENCE_REQUEST_TIMEOUT,
        )
        return self._remote_apps_from_payload(payload)

    @staticmethod
    def _remote_apps_from_payload(payload: Mapping[str, object]) -> tuple[NodeAppEntry, ...]:
        raw_apps: object | None = payload.get("apps")
        if not isinstance(raw_apps, list):
            raise RuntimeError("Remote node apps response did not include an apps list.")
        apps: list[NodeAppEntry] = []
        for raw_app in cast(Iterable[object], raw_apps):
            apps.append(
                ModWebModelsMixin._resolved_remote_app_entry(
                    NodeAppEntry.from_mapping(_json_object(raw_app, context="Remote node apps response entry"))
                )
            )
        return tuple[NodeAppEntry, ...](sorted(apps, key=lambda app: app.friendly.casefold()))

    def _remote_app_entry(self, node: ModWebNodeLink, app_name: str, user: ModWebUser) -> NodeAppEntry:
        key: str = app_name.casefold()
        for entry in self._remote_apps(node, user):
            if entry.name.casefold() == key:
                return self._resolved_remote_app_entry(entry)
        raise RuntimeError(f"Remote node did not expose app {app_name!r}.")

    async def _remote_app_entry_async(self, node: ModWebNodeLink, app_name: str, user: ModWebUser) -> NodeAppEntry:
        key: str = app_name.casefold()
        for entry in await self._remote_apps_async(node, user):
            if entry.name.casefold() == key:
                return self._resolved_remote_app_entry(entry)
        raise RuntimeError(f"Remote node did not expose app {app_name!r}.")

    def _remote_mod_list(self, node: ModWebNodeLink, app_name: str, user: ModWebUser) -> NodeModList:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/mods",
            scopes=(NodeApiScope.MODS_READ,),
            user=user,
        )
        return NodeModList.from_mapping(payload)

    def _remote_minecraft_recipe_summaries(
        self,
        node: ModWebNodeLink,
        app_name: str,
        user: ModWebUser,
    ) -> tuple[ModWebMinecraftRecipeBookSummary, ModWebMinecraftItemRegistrySummary]:
        default_recipe_summary = ModWebMinecraftRecipeBookSummary(
            data_path=".yukibot/recipes.json",
            script_path="kubejs/server_scripts/yuki_recipes.js",
        )
        default_item_registry_summary = ModWebMinecraftItemRegistrySummary(
            data_path=".yukibot/registries/items.json",
            file_exists=False,
        )
        try:
            payload: dict[str, object] = self._remote_json(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/minecraft/recipes",
                scopes=(NodeApiScope.MODS_READ,),
                user=user,
            )
            workspace_state = NodeMinecraftRecipeWorkspaceState.from_mapping(payload)
        except Exception as xcp:
            error_text = str(xcp) or type(xcp).__name__
            return (
                ModWebMinecraftRecipeBookSummary(
                    data_path=default_recipe_summary.data_path,
                    script_path=default_recipe_summary.script_path,
                    load_error=error_text,
                ),
                ModWebMinecraftItemRegistrySummary(
                    data_path=default_item_registry_summary.data_path,
                    file_exists=False,
                    load_error=error_text,
                ),
            )

        recipe_book_state = workspace_state.recipe_book
        if recipe_book_state.load_error is not None:
            recipe_summary = ModWebMinecraftRecipeBookSummary(
                data_path=recipe_book_state.data_path,
                script_path=recipe_book_state.script_path,
                load_error=recipe_book_state.load_error,
            )
        elif recipe_book_state.payload is None:
            recipe_summary = ModWebMinecraftRecipeBookSummary(
                data_path=recipe_book_state.data_path,
                script_path=recipe_book_state.script_path,
            )
        else:
            recipe_book = MinecraftRecipeBook.from_mapping(recipe_book_state.payload)
            recipe_summary = ModWebMinecraftRecipeBookSummary(
                data_path=recipe_book_state.data_path,
                script_path=recipe_book_state.script_path,
                entries=tuple(self._minecraft_recipe_entry(mutation) for mutation in recipe_book.mutations),
                mutation_mappings=tuple(mutation.to_mapping() for mutation in recipe_book.mutations),
            )

        item_registry_state = workspace_state.item_registry
        if item_registry_state.load_error is not None:
            item_registry_summary = ModWebMinecraftItemRegistrySummary(
                data_path=item_registry_state.data_path,
                file_exists=item_registry_state.file_exists,
                load_error=item_registry_state.load_error,
            )
        elif item_registry_state.payload is None:
            item_registry_summary = ModWebMinecraftItemRegistrySummary(
                data_path=item_registry_state.data_path,
                file_exists=item_registry_state.file_exists,
            )
        else:
            item_registry = MinecraftItemRegistrySnapshot.from_mapping(item_registry_state.payload)
            item_registry_summary = ModWebMinecraftItemRegistrySummary(
                data_path=item_registry_state.data_path,
                item_ids=item_registry.item_ids,
                file_exists=item_registry_state.file_exists,
                generated_at_epoch_ms=item_registry.generated_at_epoch_ms,
            )
        return recipe_summary, item_registry_summary

    def _remote_sevendays_sandbox_options_summary(
        self,
        node: ModWebNodeLink,
        app_name: str,
        user: ModWebUser,
    ) -> ModWebSevenDaysSandboxOptionsSummary:
        default_data_path = ".yukibot/sandbox_options.json"
        try:
            payload: dict[str, object] = self._remote_json(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/sevendays/sandbox-options",
                scopes=(NodeApiScope.MODS_READ,),
                user=user,
            )
            state = NodeSevenDaysSandboxOptionsState.from_mapping(payload)
        except Exception as xcp:
            return ModWebSevenDaysSandboxOptionsSummary(
                data_path=default_data_path,
                file_exists=True,
                load_error=str(xcp) or type(xcp).__name__,
            )

        if state.load_error is not None:
            return ModWebSevenDaysSandboxOptionsSummary(
                data_path=state.data_path,
                file_exists=state.file_exists,
                load_error=state.load_error,
            )
        if state.payload is None:
            return ModWebSevenDaysSandboxOptionsSummary(
                data_path=state.data_path,
                file_exists=state.file_exists,
            )
        try:
            snapshot = SevenDaysSandboxOptionsSnapshot.from_mapping(dict(cast(dict[str, object], state.payload)))
        except Exception as xcp:
            return ModWebSevenDaysSandboxOptionsSummary(
                data_path=state.data_path,
                file_exists=state.file_exists,
                load_error=str(xcp) or type(xcp).__name__,
            )
        return self._sevendays_sandbox_options_summary_from_snapshot(
            data_path=state.data_path,
            file_exists=state.file_exists,
            snapshot=snapshot,
        )

    def _remote_mod_uploads(
        self,
        node: ModWebNodeLink,
        app_name: str,
        upload_files: tuple[tuple[str, Path], ...],
        user: ModWebUser,
    ) -> NodeModUploadBatchResult:
        token: str = self._remote_token(
            node=node,
            app_name=app_name,
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
        )
        node_api_base_url = self._absolute_node_api_base_url(node.api_base_url)
        url: str = f"{node_api_base_url.rstrip('/')}/apps/{quote(app_name, safe='')}/mods/upload"
        opened_handles: list[BinaryIO] = []
        try:
            request_files: list[tuple[str, tuple[str, BinaryIO, str]]] = []
            request_data: list[tuple[str, str]] = []
            for upload_name, upload_path in upload_files:
                handle = upload_path.open("rb")
                opened_handles.append(handle)
                request_data.append(("filename", upload_name))
                request_files.append(("upload", (upload_name, handle, "application/octet-stream")))
            response: Response = requests.post(
                url,
                data=request_data,
                files=request_files,
                headers={"Authorization": f"Bearer {token}"},
                timeout=_REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as xcp:
            raise RuntimeError(f"Remote node request failed: url={url} error={type(xcp).__name__}: {xcp}") from xcp
        finally:
            for handle in opened_handles:
                handle.close()
        if response.status_code >= 400:
            raise RuntimeError(
                f"Remote node rejected the request: url={url} status={response.status_code} "
                f"detail={self._response_detail(response)}"
            )
        try:
            payload: object = cast(object, response.json())
        except ValueError as xcp:
            raise RuntimeError("Remote node returned invalid JSON.") from xcp
        return NodeModUploadBatchResult.from_mapping(_json_object(payload, context="Remote node response"))

    def _remote_config_list(self, node: ModWebNodeLink, app_name: str, user: ModWebUser) -> NodeConfigList:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/configs",
            scopes=(NodeApiScope.CONFIGS_READ,),
            user=user,
        )
        return NodeConfigList.from_mapping(payload)

    def _remote_save_list(self, node: ModWebNodeLink, app_name: str, user: ModWebUser) -> NodeSaveList:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/saves",
            scopes=(NodeApiScope.SAVES_READ,),
            user=user,
        )
        return NodeSaveList.from_mapping(payload)

    def _remote_blueprint_list(self, node: ModWebNodeLink, app_name: str, user: ModWebUser) -> NodeBlueprintList:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/blueprints",
            scopes=(NodeApiScope.BLUEPRINTS_READ,),
            user=user,
        )
        return NodeBlueprintList.from_mapping(payload)

    def _remote_save_upload(
        self,
        node: ModWebNodeLink,
        app_name: str,
        root_id: str,
        upload_path: Path,
        upload_name: str,
        user: ModWebUser,
    ) -> NodeSaveMutationResult:
        token: str = self._remote_token(
            node=node,
            app_name=app_name,
            scopes=(NodeApiScope.SAVES_WRITE,),
            user=user,
        )
        node_api_base_url = self._absolute_node_api_base_url(node.api_base_url)
        url: str = f"{node_api_base_url.rstrip('/')}/apps/{quote(app_name, safe='')}/saves/upload"
        try:
            with upload_path.open("rb") as handle:
                response: Response = requests.post(
                    url,
                    data={"root_id": root_id, "filename": upload_name},
                    files={"upload": (upload_name, handle, "application/octet-stream")},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=_REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
                )
        except requests.RequestException as xcp:
            raise RuntimeError(f"Remote node request failed: url={url} error={type(xcp).__name__}: {xcp}") from xcp
        if response.status_code >= 400:
            raise RuntimeError(
                f"Remote node rejected the request: url={url} status={response.status_code} "
                f"detail={self._response_detail(response)}"
            )
        try:
            payload: object = cast(object, response.json())
        except ValueError as xcp:
            raise RuntimeError("Remote node returned invalid JSON.") from xcp
        return NodeSaveMutationResult.from_mapping(_json_object(payload, context="Remote node response"))

    def _remote_blueprint_upload(
        self,
        node: ModWebNodeLink,
        app_name: str,
        session_name: str,
        upload_files: tuple[tuple[str, Path], ...],
        user: ModWebUser,
    ) -> NodeBlueprintMutationResult:
        token: str = self._remote_token(
            node=node,
            app_name=app_name,
            scopes=(NodeApiScope.BLUEPRINTS_WRITE,),
            user=user,
        )
        node_api_base_url = self._absolute_node_api_base_url(node.api_base_url)
        url: str = f"{node_api_base_url.rstrip('/')}/apps/{quote(app_name, safe='')}/blueprints/upload"
        opened_handles: list[BinaryIO] = []
        try:
            request_files: list[tuple[str, tuple[str, BinaryIO, str]]] = []
            for upload_name, upload_path in upload_files:
                handle = upload_path.open("rb")
                opened_handles.append(handle)
                request_files.append(("upload", (upload_name, handle, "application/octet-stream")))
            response: Response = requests.post(
                url,
                data={"session_name": session_name},
                files=request_files,
                headers={"Authorization": f"Bearer {token}"},
                timeout=_REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as xcp:
            raise RuntimeError(f"Remote node request failed: url={url} error={type(xcp).__name__}: {xcp}") from xcp
        finally:
            for handle in opened_handles:
                handle.close()
        if response.status_code >= 400:
            raise RuntimeError(
                f"Remote node rejected the request: url={url} status={response.status_code} "
                f"detail={self._response_detail(response)}"
            )
        try:
            payload: object = cast(object, response.json())
        except ValueError as xcp:
            raise RuntimeError("Remote node returned invalid JSON.") from xcp
        return NodeBlueprintMutationResult.from_mapping(_json_object(payload, context="Remote node response"))

    def _remote_save_rename(
        self,
        node: ModWebNodeLink,
        app_name: str,
        save_id: str,
        new_name: str,
        user: ModWebUser,
    ) -> NodeSaveMutationResult:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/saves/{quote(save_id, safe='/')}/rename",
            scopes=(NodeApiScope.SAVES_WRITE,),
            user=user,
            method="POST",
            json_payload={"new_name": new_name},
        )
        return NodeSaveMutationResult.from_mapping(payload)

    def _remote_save_delete(
        self,
        node: ModWebNodeLink,
        app_name: str,
        save_id: str,
        user: ModWebUser,
    ) -> NodeSaveMutationResult:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/saves/{quote(save_id, safe='/')}",
            scopes=(NodeApiScope.SAVES_WRITE,),
            user=user,
            method="DELETE",
        )
        return NodeSaveMutationResult.from_mapping(payload)

    def _remote_blueprint_delete(
        self,
        node: ModWebNodeLink,
        app_name: str,
        blueprint_id: str,
        user: ModWebUser,
    ) -> NodeBlueprintMutationResult:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/blueprints/{quote(blueprint_id, safe='/')}",
            scopes=(NodeApiScope.BLUEPRINTS_WRITE,),
            user=user,
            method="DELETE",
        )
        return NodeBlueprintMutationResult.from_mapping(payload)

    def _remote_app_runtime_summary(
        self, node: ModWebNodeLink, app_name: str, user: ModWebUser
    ) -> NodeAppRuntimeSummary:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/runtime",
            scopes=(NodeApiScope.MODS_READ,),
            user=user,
        )
        return NodeAppRuntimeSummary.from_mapping(payload)

    async def _remote_app_runtime_summary_async(
        self,
        node: ModWebNodeLink,
        app_name: str,
        user: ModWebUser,
    ) -> NodeAppRuntimeSummary:
        payload: dict[str, object] = await self._remote_json_async(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/runtime",
            scopes=(NodeApiScope.MODS_READ,),
            user=user,
        )
        return NodeAppRuntimeSummary.from_mapping(payload)

    def _remote_node_system_summary(self, node: ModWebNodeLink, user: ModWebUser) -> NodeSystemSummary:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=None,
            path="/system",
            scopes=(NodeApiScope.APPS_READ,),
            user=user,
            timeout=_REMOTE_NODE_PRESENCE_REQUEST_TIMEOUT,
        )
        return NodeSystemSummary.from_mapping(payload)

    async def _remote_node_system_summary_async(self, node: ModWebNodeLink, user: ModWebUser) -> NodeSystemSummary:
        payload: dict[str, object] = await self._remote_json_async(
            node=node,
            app_name=None,
            path="/system",
            scopes=(NodeApiScope.APPS_READ,),
            user=user,
            timeout=_REMOTE_NODE_PRESENCE_REQUEST_TIMEOUT,
        )
        return NodeSystemSummary.from_mapping(payload)

    async def _remote_node_system_summary_or_none_async(
        self,
        node: ModWebNodeLink,
        user: ModWebUser,
        *,
        error_context: str,
    ) -> NodeSystemSummary | None:
        try:
            return await self._remote_node_system_summary_async(node, user)
        except Exception as xcp:
            if not (self._shutting_down or config.IS_SHUTTINGDOWN):
                log.warning("%s: node=%s error=%s", error_context, node.node_name, xcp)
            return None

    @staticmethod
    def _app_start_blocked_remote(
        *,
        app_name: str,
        app_stats: NodeAppRuntimeSummary | None,
        start_blocked_app_ids: tuple[str, ...],
    ) -> bool:
        if app_stats is not None and app_stats.running:
            return False
        return app_name in start_blocked_app_ids

    def _remote_config_content(
        self,
        node: ModWebNodeLink,
        app_name: str,
        config_id: str,
        user: ModWebUser,
    ) -> NodeConfigContent:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/configs/{quote(config_id, safe='/')}",
            scopes=(NodeApiScope.CONFIGS_READ,),
            user=user,
        )
        return NodeConfigContent.from_mapping(payload)

    def _remote_config_write(
        self,
        node: ModWebNodeLink,
        app_name: str,
        config_id: str,
        content: str,
        user: ModWebUser,
    ) -> NodeConfigContent:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/configs/{quote(config_id, safe='/')}",
            scopes=(NodeApiScope.CONFIGS_WRITE,),
            user=user,
            method="PUT",
            json_payload={"content": content},
        )
        return NodeConfigContent.from_mapping(payload)

    def _remote_setting_list(self, node: ModWebNodeLink, app_name: str, user: ModWebUser) -> NodeSettingList:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/settings",
            scopes=(NodeApiScope.SETTINGS_READ,),
            user=user,
        )
        return NodeSettingList.from_mapping(payload)

    def _remote_setting_write(
        self,
        node: ModWebNodeLink,
        app_name: str,
        setting_key: str,
        value: str,
        user: ModWebUser,
    ) -> NodeSettingMutationResult:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/settings/{quote(setting_key, safe='')}",
            scopes=(NodeApiScope.SETTINGS_WRITE,),
            user=user,
            method="PUT",
            json_payload={"value": value},
        )
        return NodeSettingMutationResult.from_mapping(payload)

    def _remote_settings_save(self, node: ModWebNodeLink, app_name: str, user: ModWebUser) -> NodeSettingsActionResult:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/settings/save",
            scopes=(NodeApiScope.SETTINGS_WRITE,),
            user=user,
            method="POST",
        )
        return NodeSettingsActionResult.from_mapping(payload)

    def _remote_settings_reload(
        self, node: ModWebNodeLink, app_name: str, user: ModWebUser
    ) -> NodeSettingsActionResult:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/settings/reload",
            scopes=(NodeApiScope.SETTINGS_WRITE,),
            user=user,
            method="POST",
        )
        return NodeSettingsActionResult.from_mapping(payload)

    def _remote_console_action_list(
        self,
        node: ModWebNodeLink,
        app_name: str,
        user: ModWebUser,
    ) -> NodeConsoleActionList:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/console-actions",
            scopes=(NodeApiScope.APP_CONTROL,),
            user=user,
        )
        return NodeConsoleActionList.from_mapping(payload)

    def _remote_console_stdout(
        self,
        node: ModWebNodeLink,
        app_name: str,
        *,
        max_lines: int,
        user: ModWebUser,
    ) -> NodeConsoleStdoutSnapshot:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/console/stdout?max_lines={max_lines}",
            scopes=(NodeApiScope.APP_CONTROL,),
            user=user,
            timeout=_REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
        )
        return NodeConsoleStdoutSnapshot.from_mapping(payload)

    def _remote_execute_console_action(
        self,
        node: ModWebNodeLink,
        app_name: str,
        action_key: str,
        raw_value: str | None,
        user: ModWebUser,
    ) -> NodeConsoleActionExecutionResult:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/console-actions/{quote(action_key, safe='')}",
            scopes=(NodeApiScope.APP_CONTROL,),
            user=user,
            method="POST",
            json_payload={"value": raw_value},
        )
        return NodeConsoleActionExecutionResult.from_mapping(payload)

    def _remote_json(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str | None,
        path: str,
        scopes: tuple[NodeApiScope, ...],
        user: ModWebUser,
        method: str = "GET",
        json_payload: Mapping[str, object] | None = None,
        timeout: float | tuple[float, float] = _REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        token: str = self._remote_token(node=node, app_name=app_name, scopes=scopes, user=user)
        node_api_base_url = self._absolute_node_api_base_url(node.api_base_url)
        url: str = f"{node_api_base_url.rstrip('/')}/{path.lstrip('/')}"
        try:
            if method == "GET":
                response = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=timeout,
                )
            elif method == "PUT":
                response = requests.put(
                    url,
                    json=_json_request_object(json_payload),
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=timeout,
                )
            elif method == "POST":
                response: Response = requests.post(
                    url,
                    json=_json_request_object(json_payload),
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=timeout,
                )
            elif method == "DELETE":
                response = requests.delete(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=timeout,
                )
            else:
                raise ValueError(f"Unsupported remote node request method: {method}")
        except requests.RequestException as xcp:
            raise RuntimeError(f"Remote node request failed: url={url} error={type(xcp).__name__}: {xcp}") from xcp
        if response.status_code >= 400:
            raise RuntimeError(
                f"Remote node rejected the request: url={url} status={response.status_code} "
                f"detail={self._response_detail(response)}"
            )
        try:
            payload: object = cast(object, response.json())
        except ValueError as xcp:
            raise RuntimeError("Remote node returned invalid JSON.") from xcp
        return _json_object(payload, context="Remote node response")

    async def _remote_json_async(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str | None,
        path: str,
        scopes: tuple[NodeApiScope, ...],
        user: ModWebUser,
        method: str = "GET",
        json_payload: Mapping[str, object] | None = None,
        timeout: float | tuple[float, float] = _REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        token: str = self._remote_token(node=node, app_name=app_name, scopes=scopes, user=user)
        node_api_base_url = self._absolute_node_api_base_url(node.api_base_url)
        url: str = f"{node_api_base_url.rstrip('/')}/{path.lstrip('/')}"
        try:
            async with aiohttp.ClientSession(timeout=self._aiohttp_client_timeout(timeout)) as session:
                if method == "GET":
                    response_context = session.get(url, headers={"Authorization": f"Bearer {token}"})
                else:
                    response_context = session.request(
                        method,
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        json=_json_request_object(json_payload),
                    )
                async with response_context as response:
                    if response.status >= 400:
                        raise RuntimeError(
                            f"Remote node rejected the request: url={url} status={response.status} "
                            f"detail={await self._aiohttp_response_detail(response)}"
                        )
                    try:
                        payload: object = cast(object, await response.json())
                    except (aiohttp.ContentTypeError, ValueError, json.JSONDecodeError) as xcp:
                        raise RuntimeError("Remote node returned invalid JSON.") from xcp
        except (aiohttp.ClientError, asyncio.TimeoutError) as xcp:
            raise RuntimeError(f"Remote node request failed: url={url} error={type(xcp).__name__}: {xcp}") from xcp
        return _json_object(payload, context="Remote node response")

    async def _remote_bytes_async(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str | None,
        path: str,
        scopes: tuple[NodeApiScope, ...],
        user: ModWebUser,
        timeout: float | tuple[float, float] = _REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
    ) -> tuple[bytes, str | None, tuple[tuple[str, str], ...]]:
        token: str = self._remote_token(node=node, app_name=app_name, scopes=scopes, user=user)
        node_api_base_url = self._absolute_node_api_base_url(node.api_base_url)
        url: str = f"{node_api_base_url.rstrip('/')}/{path.lstrip('/')}"
        try:
            async with aiohttp.ClientSession(timeout=self._aiohttp_client_timeout(timeout)) as session:
                async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as response:
                    if response.status >= 400:
                        raise RuntimeError(
                            f"Remote node rejected the request: url={url} status={response.status} "
                            f"detail={await self._aiohttp_response_detail(response)}"
                        )
                    content = await response.read()
                    media_type = response.headers.get("Content-Type")
                    forwarded_headers = tuple(
                        (header_name, header_value)
                        for header_name in ("Cache-Control", "ETag", "Last-Modified", "Expires")
                        if (header_value := response.headers.get(header_name))
                    )
                    return content, media_type, forwarded_headers
        except (aiohttp.ClientError, asyncio.TimeoutError) as xcp:
            raise RuntimeError(f"Remote node request failed: url={url} error={type(xcp).__name__}: {xcp}") from xcp

    @staticmethod
    def _aiohttp_client_timeout(timeout: float | tuple[float, float]) -> aiohttp.ClientTimeout:
        if isinstance(timeout, tuple):
            connect_timeout, read_timeout = timeout
            return aiohttp.ClientTimeout(
                total=connect_timeout + read_timeout,
                connect=connect_timeout,
                sock_connect=connect_timeout,
                sock_read=read_timeout,
            )
        return aiohttp.ClientTimeout(
            total=timeout,
            connect=timeout,
            sock_connect=timeout,
            sock_read=timeout,
        )

    @staticmethod
    async def _aiohttp_response_detail(response: aiohttp.ClientResponse) -> str:
        response_text: str = await response.text()
        try:
            payload: object = cast(object, json.loads(response_text))
        except ValueError:
            return response_text or response.reason or ""
        if isinstance(payload, Mapping):
            detail: object | None = cast(Mapping[object, object], payload).get("detail")
            if isinstance(detail, str) and detail:
                return detail
        return response_text or response.reason or ""

    @staticmethod
    async def _persist_uploaded_file(upload_file: "FileUpload") -> Path:
        suffix: str = Path(upload_file.name).suffix
        with tempfile.NamedTemporaryFile(prefix="yukibot-save-web-", suffix=suffix, delete=False) as handle:
            temp_path: Path = Path(handle.name)
        await upload_file.save(temp_path)
        return temp_path

    def _remote_download_redirect(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str,
        path: str,
        query: Mapping[str, object],
        user: ModWebUser,
        scopes: tuple[NodeApiScope, ...] = (NodeApiScope.MODS_DOWNLOAD,),
    ) -> RedirectResponse:
        return RedirectResponse(
            self._remote_download_url(node=node, app_name=app_name, path=path, query=query, user=user, scopes=scopes),
            status_code=302,
        )

    def _remote_download_url(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str,
        path: str,
        query: Mapping[str, object],
        user: ModWebUser,
        scopes: tuple[NodeApiScope, ...] = (NodeApiScope.MODS_DOWNLOAD,),
    ) -> str:
        token: str = self._remote_token(
            node=node,
            app_name=app_name,
            scopes=scopes,
            user=user,
        )
        query_with_token: dict[str, object] = dict[str, object](query)
        query_with_token["access_token"] = token
        node_api_base_url = self._absolute_node_api_base_url(node.api_base_url)
        return f"{node_api_base_url.rstrip('/')}/{path.lstrip('/')}?{urlencode(query_with_token, doseq=True)}"

    def _remote_token(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
        user: ModWebUser,
    ) -> str:
        secret: str | None = config.MOD_WEB_SERVER.token_secret
        if secret is None:
            raise RuntimeError("NODE_API_TOKEN_SECRET or DATA_AUTHORITY_TOKEN is required to proxy remote nodes.")
        return issue_node_token(
            secret=secret,
            grant=NodeAccessGrant(
                subject=f"web:{user.discord_id}",
                node=node.node_name,
                app=app_name,
                scopes=frozenset[NodeApiScope](scopes),
                expires_at=int(time.time()) + _REMOTE_NODE_TOKEN_TTL_SECONDS,
            ),
        )

    def _require_http_user(self, *, request: Request, required_level: Power_Level) -> ModWebUser:
        if not self._auth.enabled:
            raise _http_exception(503, "Discord OAuth is not configured for the mod web UI.")
        user: ModWebUser | None = self._auth.current_user(request)
        if user is None:
            raise _http_exception(401, "Discord login is required.")
        if self._acl is None:
            raise _http_exception(503, "Mod web permissions are not available.")
        if not self._acl.can(user.discord_id, required_level):
            raise _http_exception(
                403,
                f"Insufficient level: {self._acl.level_of(user.discord_id).name.title()} < {required_level.name.title()}",
            )
        return user

    @staticmethod
    def _download_query(*, enabled_only: bool, selected_only: bool, mod_names: tuple[str, ...]) -> dict[str, object]:
        query: dict[str, object] = {
            "enabled_only": str(enabled_only).lower(),
            "selected_only": str(selected_only).lower(),
        }
        if mod_names:
            query["mod_name"] = list[str](mod_names)
        return query

    def _remote_portal_redirect(self, request: Request) -> RedirectResponse | None:
        if config.DATA_AUTHORITY_MODE is not config.DataAuthorityMode.REMOTE:
            return None
        if request.method not in {"GET", "HEAD"}:
            return None

        if config.ACTIVE_BOT_PROFILE.name is config.BotProfileName.PORTAL:
            target_node_name = self._portal_default_node_name()
            if target_node_name is None:
                return None
            target_path = self._remote_portal_path(request.url.path, target_node_name=target_node_name)
            if target_path is None:
                return None
            query: str = request.url.query
            if query and "?" in target_path:
                suffix = f"&{query}"
            elif query:
                suffix = f"?{query}"
            else:
                suffix = ""
            return RedirectResponse(f"{target_path}{suffix}", status_code=302)

        target_path = self._remote_portal_path(request.url.path, target_node_name=config.MOD_WEB_SERVER.node_name)
        if target_path is None:
            return None
        base_url: str | None = self._portal_base_url()
        if base_url is None:
            log.warning("Remote mod web redirect skipped because the portal URL is unknown.")
            return None
        query = request.url.query
        if query and "?" in target_path:
            suffix = f"&{query}"
        elif query:
            suffix = f"?{query}"
        else:
            suffix = ""
        return RedirectResponse(f"{base_url.rstrip('/')}{target_path}{suffix}", status_code=302)

    def _remote_portal_path(self, path: str, *, target_node_name: str) -> str | None:
        if path.startswith("/api/node"):
            return None
        if path.startswith("/_nicegui") or path.startswith("/static"):
            return None
        is_portal_profile: bool = config.ACTIVE_BOT_PROFILE.name is config.BotProfileName.PORTAL
        if path.startswith("/auth/"):
            if is_portal_profile:
                return None
            node_path = mod_web_node_path(target_node_name)
            return f"/auth/login?{urlencode({'next_path': node_path})}"
        if path.startswith(self._NODE_SCOPED_PATH_PREFIX):
            if is_portal_profile:
                return None
            return path
        if path.startswith(self._PORTAL_OWNED_PATH_PREFIXES):
            if is_portal_profile:
                return None
            return path

        node_path: str = mod_web_node_path(target_node_name)
        if path in {"", "/", "/mod-web"}:
            if is_portal_profile:
                return None
            return node_path

        chat_app_name: str | None = self._chat_app_name_from_remote_ui_path(path)
        if chat_app_name is not None:
            return f"{node_path}/chat/{quote(chat_app_name, safe='')}"

        app_name: str | None = self._app_name_from_remote_ui_path(path)
        if app_name is not None:
            return f"{node_path}/mods/{quote(app_name, safe='')}"

        if is_portal_profile and path.startswith("/mod-web"):
            return None
        if (
            path.startswith("/mod-web")
            or path.startswith("/apps/")
            or path.startswith("/app/")
            or path.startswith("/mods/")
        ):
            return node_path
        return None

    @staticmethod
    def _app_name_from_remote_ui_path(path: str) -> str | None:
        prefixes: tuple[
            Literal["/apps/"], Literal["/app/"], Literal["/mods/"], Literal["/mod-web/apps/"], Literal["/mod-web/mods/"]
        ] = (
            "/apps/",
            "/app/",
            "/mods/",
            "/mod-web/apps/",
            "/mod-web/mods/",
        )
        for prefix in prefixes:
            if path.startswith(prefix):
                app_name = path[len(prefix) :].strip("/")
                return app_name or None
        return None

    @staticmethod
    def _chat_app_name_from_remote_ui_path(path: str) -> str | None:
        prefixes: tuple[Literal["/chat/"], Literal["/mod-web/chat/"]] = (
            "/chat/",
            "/mod-web/chat/",
        )
        for prefix in prefixes:
            if path.startswith(prefix):
                app_name = path[len(prefix) :].strip("/")
                return app_name or None
        return None

    def _portal_base_url(self) -> str | None:
        for snapshot in self._known_bot_snapshots():
            if snapshot.profile.bot_profile is not config.BotProfileName.PORTAL:
                continue
            mod_web: BotMetadataModWeb | None = snapshot.features.mod_web
            if mod_web is not None:
                return mod_web.public_base_url

        for snapshot in self._known_bot_snapshots():
            if snapshot.profile.bot_profile is not config.BotProfileName.YUKI:
                continue
            mod_web: BotMetadataModWeb | None = snapshot.features.mod_web
            if mod_web is not None:
                return mod_web.public_base_url

        endpoint: AuthorityEndpoint | None = config.DATA_AUTHORITY_ENDPOINT
        if endpoint is not None:
            return urlunsplit((endpoint.scheme, f"{endpoint.host}:{endpoint.port}", "", "", ""))
        return None

    @staticmethod
    def _response_detail(response: requests.Response) -> str:
        try:
            payload: object = cast(object, response.json())
        except ValueError:
            return response.text or response.reason
        if isinstance(payload, Mapping):
            detail: object | None = cast(Mapping[object, object], payload).get("detail")
            if isinstance(detail, str) and detail:
                return detail
        return response.text or response.reason

    @staticmethod
    def _known_bot_snapshots() -> tuple[config.BotMetadataSnapshot, ...]:
        snapshots: list[config.BotMetadataSnapshot] = []
        try:
            snapshots.extend(config.load_bot_configuration(Path("configuration.json")).known_bots.values())
        except Exception as xcp:
            log.warning("Mod web failed to load local bot registry: %s", xcp)

        cache_path: Path = config.authority_cache_path(AuthorityResource.BOTS)
        if cache_path.exists():
            try:
                raw_cache: dict[str, object] = read_json_object(cache_path)
                snapshots.extend(
                    config.BotMetadataSnapshot.model_validate(snapshot)
                    for snapshot in raw_cache.values()
                    if isinstance(snapshot, dict)
                )
            except Exception as xcp:
                log.warning("Mod web failed to load cached bot registry: %s", xcp)

        unique: dict[str, config.BotMetadataSnapshot] = {}
        for snapshot in snapshots:
            unique[snapshot.profile.id] = snapshot
        return tuple[BotMetadataSnapshot, ...](unique.values())

    def _app_start_blocked_local(self, app: App) -> bool:
        manager: App_Manager | None = self._manager
        if manager is None:
            return False
        return manager.start_blocker(app, include_current_activity=False) is not None

    @staticmethod
    def _percent_tone(percent: int) -> BadgeTone:
        if percent >= 90:
            return "red"
        if percent >= 75:
            return "warn"
        if percent >= 40:
            return "purple"
        return "grey"

    @classmethod
    def _running_tone(cls, running_count: int, total_count: int) -> BadgeTone:
        if total_count <= 0 or running_count <= 0:
            return "warn"
        if running_count < total_count:
            return "grey"
        return "purple"

    @staticmethod
    def _running_value(running_names: tuple[str, ...]) -> str:
        if not running_names:
            return "None"
        preview: tuple[str, ...] = running_names[:2]
        if len(running_names) <= len(preview):
            return ", ".join(preview)
        return f"{', '.join(preview)} +{len(running_names) - len(preview)}"

    @staticmethod
    def _storage_value(*, percent: int, free_bytes: int, total_bytes: int) -> str:
        free_text: str = Utilities.humanise_bytes(free_bytes, precision=1)
        total_text: str = Utilities.humanise_bytes(total_bytes, precision=1)
        return f"{percent}% · {free_text} / {total_text}"

    @staticmethod
    def _app_footprint_value(size_bytes: int) -> str:
        return f"{Utilities.humanise_bytes(size_bytes, precision=1)}"

    @staticmethod
    def _ram_value(*, percent: int, used_bytes: int, total_bytes: int) -> str:
        used_text: str = Utilities.humanise_bytes(used_bytes, precision=1)
        total_text: str = Utilities.humanise_bytes(total_bytes, precision=1)
        return f"{percent}% · {used_text} / {total_text}"

    @classmethod
    def _system_running_entry(
        cls, *, system_summary: NodeSystemSummary | None, app_count: int
    ) -> tuple[str, BadgeTone]:
        if system_summary is None:
            return ("Unavailable", "red")
        running_names = system_summary.running_names
        return (cls._running_value(running_names), cls._running_tone(len(running_names), app_count))

    @classmethod
    def _system_cpu_entry(cls, system_summary: NodeSystemSummary | None) -> tuple[str, BadgeTone]:
        if system_summary is None:
            return ("Unavailable", "red")
        if system_summary.cpu_percent is None:
            return ("Unavailable", "grey")
        return (f"{system_summary.cpu_percent}%", cls._percent_tone(system_summary.cpu_percent))

    @classmethod
    def _system_ram_entry(cls, system_summary: NodeSystemSummary | None) -> tuple[str, BadgeTone]:
        if system_summary is None:
            return ("Unavailable", "red")
        if (
            system_summary.ram_percent is None
            or system_summary.ram_used_bytes is None
            or system_summary.ram_total_bytes is None
        ):
            return ("Unavailable", "grey")
        return (
            cls._ram_value(
                percent=system_summary.ram_percent,
                used_bytes=system_summary.ram_used_bytes,
                total_bytes=system_summary.ram_total_bytes,
            ),
            cls._percent_tone(system_summary.ram_percent),
        )

    @classmethod
    def _system_storage_entry(cls, system_summary: NodeSystemSummary | None) -> tuple[str, BadgeTone]:
        if system_summary is None:
            return ("Unavailable", "red")
        if (
            system_summary.storage_percent is None
            or system_summary.storage_free_bytes is None
            or system_summary.storage_total_bytes is None
        ):
            return ("Unavailable", "grey")
        return (
            cls._storage_value(
                percent=system_summary.storage_percent,
                free_bytes=system_summary.storage_free_bytes,
                total_bytes=system_summary.storage_total_bytes,
            ),
            cls._percent_tone(system_summary.storage_percent),
        )

    @classmethod
    def _system_uptime_entry(cls, system_summary: NodeSystemSummary | None) -> tuple[str, BadgeTone]:
        if system_summary is None:
            return ("Unavailable", "red")
        bot_uptime_seconds: int | None = system_summary.bot_uptime_seconds
        system_uptime_seconds: int | None = system_summary.uptime_seconds
        if bot_uptime_seconds is None and system_uptime_seconds is None:
            return ("Unavailable", "grey")
        if bot_uptime_seconds is None:
            if system_uptime_seconds is None:
                raise RuntimeError("System uptime unexpectedly missing.")
            return (cls._format_uptime_seconds(system_uptime_seconds), "black")
        if system_uptime_seconds is None:
            return (cls._format_uptime_seconds(bot_uptime_seconds), "black")
        return (
            f"{cls._format_uptime_seconds(bot_uptime_seconds)} | {cls._format_uptime_seconds(system_uptime_seconds)}",
            "black",
        )

    @staticmethod
    def _format_uptime_seconds(total_seconds: int) -> str:
        remaining = max(0, int(total_seconds))
        days, remaining = divmod(remaining, 24 * 60 * 60)
        hours, remaining = divmod(remaining, 60 * 60)
        minutes, _seconds = divmod(remaining, 60)
        if days == 0 and hours == 0 and minutes == 0:
            return "<1m"
        parts: list[str] = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0 or parts:
            parts.append(f"{hours}h")
        if minutes > 0 or parts:
            parts.append(f"{minutes}m")
        return " ".join(parts)

    @staticmethod
    def _dominant_tone(tones: tuple[BadgeTone, ...]) -> BadgeTone:
        if not tones:
            return "grey"
        rank: dict[BadgeTone, int] = {
            "black": 0,
            "grey": 1,
            "purple": 2,
            "warn": 3,
            "red": 4,
        }
        return max(tones, key=lambda tone: rank[tone])

    @classmethod
    def _build_system_title_stats(cls, system_summary: NodeSystemSummary | None) -> tuple[ModWebTitleStat, ...]:
        cpu_value, cpu_tone = cls._system_cpu_entry(system_summary)
        ram_value, ram_tone = cls._system_ram_entry(system_summary)
        storage_value, storage_tone = cls._system_storage_entry(system_summary)

        return (
            ModWebTitleStat(label="CPU", value=cpu_value, tone=cpu_tone),
            ModWebTitleStat(label="RAM", value=ram_value, tone=ram_tone),
            ModWebTitleStat(label="Storage", value=storage_value, tone=storage_tone),
        )

    async def _home_node_summaries(
        self,
        *,
        sections: tuple[ModWebNodeAppSection, ...],
        user: ModWebUser,
    ) -> tuple[ModWebHomeNodeSummary, ...]:
        summaries: list[ModWebHomeNodeSummary | None] = [None] * len(sections)
        remote_jobs: list[tuple[int, ModWebNodeAppSection]] = []
        for index, section in enumerate[ModWebNodeAppSection](sections):
            if section.error is not None:
                summaries[index] = ModWebHomeNodeSummary(
                    node=section.node,
                    app_count=len(section.app_links),
                    system_summary=None,
                )
            else:
                remote_jobs.append((index, section))

        async def _remote_summary(index: int, section: ModWebNodeAppSection) -> tuple[int, ModWebHomeNodeSummary]:
            system_summary = await self._remote_node_system_summary_or_none_async(
                section.node,
                user,
                error_context="Remote mod web home system summary failed",
            )
            return (
                index,
                ModWebHomeNodeSummary(
                    node=section.node,
                    app_count=len(section.app_links),
                    system_summary=system_summary,
                ),
            )

        for index, summary in await asyncio.gather(
            *(_remote_summary(index, section) for index, section in remote_jobs)
        ):
            summaries[index] = summary

        return tuple[ModWebHomeNodeSummary, ...](summary for summary in summaries if summary is not None)

    @classmethod
    def _build_home_title_stats(cls, node_summaries: tuple[ModWebHomeNodeSummary, ...]) -> tuple[ModWebTitleStat, ...]:
        running_lines: list[ModWebTitleStatLine] = []
        running_tones: list[BadgeTone] = []
        cpu_lines: list[ModWebTitleStatLine] = []
        cpu_tones: list[BadgeTone] = []
        ram_lines: list[ModWebTitleStatLine] = []
        ram_tones: list[BadgeTone] = []
        storage_lines: list[ModWebTitleStatLine] = []
        storage_tones: list[BadgeTone] = []
        uptime_lines: list[ModWebTitleStatLine] = []
        uptime_tones: list[BadgeTone] = []

        for node_summary in node_summaries:
            node_label: str = node_summary.node.label
            running_value, running_tone = cls._system_running_entry(
                system_summary=node_summary.system_summary,
                app_count=node_summary.app_count,
            )
            cpu_value, cpu_tone = cls._system_cpu_entry(node_summary.system_summary)
            ram_value, ram_tone = cls._system_ram_entry(node_summary.system_summary)
            storage_value, storage_tone = cls._system_storage_entry(node_summary.system_summary)
            uptime_value, uptime_tone = cls._system_uptime_entry(node_summary.system_summary)

            running_lines.append(ModWebTitleStatLine(label=node_label, value=running_value))
            running_tones.append(running_tone)
            cpu_lines.append(ModWebTitleStatLine(label=None, value=cpu_value))
            cpu_tones.append(cpu_tone)
            ram_lines.append(ModWebTitleStatLine(label=None, value=ram_value))
            ram_tones.append(ram_tone)
            storage_lines.append(ModWebTitleStatLine(label=None, value=storage_value))
            storage_tones.append(storage_tone)
            uptime_lines.append(ModWebTitleStatLine(label=None, value=uptime_value))
            uptime_tones.append(uptime_tone)

        return (
            ModWebTitleStat(
                label="Running",
                value="Unavailable",
                tone=cls._dominant_tone(tuple[BadgeTone, ...](running_tones)),
                lines=tuple[ModWebTitleStatLine, ...](running_lines),
            ),
            ModWebTitleStat(
                label="CPU",
                value="Unavailable",
                tone=cls._dominant_tone(tuple[BadgeTone, ...](cpu_tones)),
                lines=tuple[ModWebTitleStatLine, ...](cpu_lines),
            ),
            ModWebTitleStat(
                label="RAM",
                value="Unavailable",
                tone=cls._dominant_tone(tuple[BadgeTone, ...](ram_tones)),
                lines=tuple[ModWebTitleStatLine, ...](ram_lines),
            ),
            ModWebTitleStat(
                label="Storage",
                value="Unavailable",
                tone=cls._dominant_tone(tuple[BadgeTone, ...](storage_tones)),
                lines=tuple[ModWebTitleStatLine, ...](storage_lines),
            ),
            ModWebTitleStat(
                label="Uptime",
                value="Unavailable",
                tone=cls._dominant_tone(tuple[BadgeTone, ...](uptime_tones)),
                lines=tuple[ModWebTitleStatLine, ...](uptime_lines),
            ),
        )

    @classmethod
    def _build_app_title_stats(cls, app_stats: NodeAppRuntimeSummary | None) -> tuple[ModWebTitleStat, ...]:
        if app_stats is None:
            status_value = "Unknown"
            status_tone: BadgeTone = "grey"
            relay_value = "Unknown"
            relay_tone: BadgeTone = "grey"
            version_value = "Unknown"
            storage_value = "Unavailable"
            storage_tone: BadgeTone = "grey"
        else:
            if app_stats.running:
                status_value = "Running"
                status_tone = "purple"
            elif not app_stats.enabled:
                status_value = "Disabled"
                status_tone = "red"
            elif app_stats.runtime_fault is not None:
                status_value = "Crashed"
                status_tone = "red"
            else:
                status_value = "Stopped"
                status_tone = "warn"

            relay_value = app_stats.relay_support.display_value
            relay_tone = "grey"
            version_value = app_stats.version or "Unknown"
            if app_stats.transition_state is NodeAppTransitionState.STOPPING:
                status_value = "Stopping"
                status_tone = "warn"
            else:
                player_snapshot_text: str | None = ModWebHomeMixin._player_count_snapshot_text(
                    player_count=app_stats.player_count,
                    player_capacity=app_stats.player_capacity,
                )
                if app_stats.running and player_snapshot_text is not None:
                    if app_stats.player_count is None:
                        raise RuntimeError("Player count unexpectedly missing for running app snapshot.")
                    status_value: str = player_snapshot_text
                    status_tone = "purple" if app_stats.player_count > 0 else "grey"
            if app_stats.transition_state is NodeAppTransitionState.STARTING:
                status_value = "Starting"
                status_tone = "purple"
            if app_stats.footprint_bytes is not None:
                storage_value: str = cls._app_footprint_value(app_stats.footprint_bytes)
                storage_tone = "grey"
            else:
                storage_value = "Unavailable"
                storage_tone = "grey"

        return (
            ModWebTitleStat(label="Status", value=status_value, tone=status_tone),
            ModWebTitleStat(label="Relay", value=relay_value, tone=relay_tone),
            ModWebTitleStat(label="Version", value=version_value, tone="black"),
            ModWebTitleStat(label="Storage", value=storage_value, tone=storage_tone),
        )

    def _render_live_title_stats_renderer(
        self,
        *,
        ui: ModWebUi,
        initial_stats: tuple[ModWebTitleStat, ...],
    ) -> Callable[[tuple[ModWebTitleStat, ...]], None]:
        @ui.refreshable
        def _render_stats(stats: tuple[ModWebTitleStat, ...]) -> None:
            with ui.row().classes("mod-stat-grid w-full gap-2 flex-wrap"):
                for stat in stats:
                    with ui.card().classes(f"mod-stat-card {stat.tone} min-w-36 grow"):
                        with ui.column().classes("gap-1 p-2"):
                            ui.label(stat.label).classes("mod-stat-label")
                            if stat.lines:
                                with ui.column().classes("gap-1"):
                                    for line in stat.lines:
                                        if line.label is None:
                                            ui.label(line.value).classes("mod-stat-line-value break-all")
                                        else:
                                            with ui.row().classes(
                                                "mod-stat-line w-full items-start justify-between gap-3"
                                            ):
                                                ui.label(line.label).classes("mod-stat-line-label")
                                                ui.label(line.value).classes("mod-stat-line-value break-all text-right")
                            else:
                                ui.label(stat.value).classes("mod-stat-value break-all")

        _render_stats(initial_stats)
        return _render_stats.refresh

    def _render_live_title_stats(
        self,
        *,
        ui: ModWebUi,
        initial_stats: tuple[ModWebTitleStat, ...],
        refresh_stats: Callable[[], tuple[ModWebTitleStat, ...]] | None = None,
        refresh_async_stats: Callable[[], Awaitable[tuple[ModWebTitleStat, ...]]] | None = None,
    ) -> Callable[[tuple[ModWebTitleStat, ...]], None]:
        if refresh_stats is not None and refresh_async_stats is not None:
            raise ValueError("Only one title stats refresh callback may be provided.")

        apply_stats: Callable[[tuple[ModWebTitleStat, ...]], None] = self._render_live_title_stats_renderer(
            ui=ui, initial_stats=initial_stats
        )

        if refresh_stats is not None:
            refresh_timer: Timer = ui.timer(
                _TITLE_STATS_REFRESH_INTERVAL_SECONDS,
                lambda: apply_stats(refresh_stats()),
            )
            self._register_timer_cleanup(ui=ui, timer=refresh_timer)
        if refresh_async_stats is not None:
            refresh_async: AsyncRefresh = self._build_async_refreshable_updater(
                refresh_async_value=refresh_async_stats,
                apply_value=apply_stats,
                error_context="Mod web title stats",
            )
            refresh_async_timer: Timer = ui.timer(
                _TITLE_STATS_REFRESH_INTERVAL_SECONDS,
                lambda: asyncio.create_task(refresh_async()),
            )
            self._register_timer_cleanup(ui=ui, timer=refresh_async_timer)
        return apply_stats

    def _build_async_refreshable_updater(
        self,
        *,
        refresh_async_value: Callable[[], Awaitable[RefreshableValue]],
        apply_value: Callable[[RefreshableValue], None],
        error_context: str,
    ) -> AsyncRefresh:
        refresh_in_flight = False

        async def _refresh_async() -> None:
            nonlocal refresh_in_flight
            if self._shutting_down or config.IS_SHUTTINGDOWN:
                return
            if refresh_in_flight:
                return
            refresh_in_flight = True
            try:
                apply_value(await refresh_async_value())
            except Exception as xcp:
                if self._shutting_down or config.IS_SHUTTINGDOWN:
                    return
                if _is_executor_shutdown_error(xcp):
                    return
                log.warning("%s refresh failed: %s", error_context, xcp)
            finally:
                refresh_in_flight = False

        return _refresh_async

    @staticmethod
    def _build_async_title_stats_refresher(
        *,
        refresh_async_stats: Callable[[], Awaitable[tuple[ModWebTitleStat, ...]]],
        apply_stats: Callable[[tuple[ModWebTitleStat, ...]], None],
    ) -> AsyncRefresh:
        refresh_in_flight = False

        async def _refresh_async() -> None:
            nonlocal refresh_in_flight
            if refresh_in_flight:
                return
            refresh_in_flight = True
            try:
                apply_stats(await refresh_async_stats())
            except Exception as xcp:
                log.warning("Mod web title stats refresh failed: %s", xcp)
            finally:
                refresh_in_flight = False

        return _refresh_async

    @staticmethod
    def _hero_card_classes() -> str:
        return "mod-card mod-card-hero w-full"

    @staticmethod
    def _app_runtime_state_class(
        *,
        running: bool,
        transition_state: NodeAppTransitionState,
        class_prefix: str,
    ) -> str | None:
        state_suffix: Literal["starting", "running", "stopping"] | None = None
        if transition_state is NodeAppTransitionState.STARTING:
            state_suffix = "starting"
        elif transition_state is NodeAppTransitionState.STOPPING:
            state_suffix = "stopping"
        elif running:
            state_suffix = "running"
        if state_suffix is None:
            return None
        return f"{class_prefix}-{state_suffix}"

    @staticmethod
    def _hero_shell_classes() -> str:
        return "mod-hero-shell gap-2 px-2 pb-2 pt-2 md:px-5 md:pb-5 md:pt-4"

    @staticmethod
    def _app_page_classes() -> str:
        return "mod-page mod-page-app w-full gap-5 px-3 py-6 md:px-5"

    @staticmethod
    def _app_page_hero_shell_classes() -> str:
        return "mod-hero-shell gap-2 px-2 pb-2 pt-2 md:px-4 md:pb-4 md:pt-3"

    @staticmethod
    def _hero_header_classes() -> str:
        return "mod-hero-header w-full items-start justify-between gap-3"

    @staticmethod
    def _hero_header_main_classes() -> str:
        return "mod-hero-header-main gap-1 mod-hero-app-title-block"

    @staticmethod
    def _hero_badges_classes(*, wide: bool = False) -> str:
        classes = "mod-corner-badges"
        if wide:
            return f"{classes} mod-corner-badges-wide"
        return classes

    @staticmethod
    def _hero_badge_row_classes(*, fill: bool = False) -> str:
        classes = "mod-corner-badge-row"
        if fill:
            return f"{classes} mod-corner-badge-row-fill"
        return classes

    @staticmethod
    def _hero_title_classes() -> str:
        return "mod-title text-3xl md:text-5xl font-black tracking-tight"

    @staticmethod
    def _hero_support_classes() -> str:
        return "mod-hero-support text-base mod-subtitle"

    @staticmethod
    def _hero_action_row_classes() -> str:
        return "mod-hero-actions"
