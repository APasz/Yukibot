from __future__ import annotations

from . import avatars as mod_web_avatars
from .actions import ModWebActionsMixin
from .app_page import ModWebAppPageMixin
from .backend import ModWebDashboardBackend
from .chat import ModWebChatMixin
from .constants import (
    _MOD_WEB_STARTUP_TIMEOUT_SECONDS,
    log,
)
from .editors import ModWebEditorsMixin
from .home import ModWebHomeMixin
from .models import ModWebModelsMixin
from .nicegui_protocols import (
    ModWebFastApiApp,
    ModWebRouteUi,
    ModWebRunnerUi,
    WebChatRelayPublisher,
    _cast_mod_web_route_ui,
)
from .page_handlers import ModWebPageHandlersMixin
from .routes import ModWebRoutesMixin
from .runtime_imports import (
    Access_Control,
    App,
    App_Manager,
    ModWebUser,
    RelayTTSQueue,
    asyncio,
    cast,
    config,
    escape,
    quote,
    threading,
)
from .status import ModWebStatusMixin
from .streams import ModWebStreamsMixin
from .tabs import ModWebTabsMixin
from .ui_helpers import ModWebUiHelpersMixin


class ModWebService(
    ModWebRoutesMixin,
    ModWebPageHandlersMixin,
    ModWebStreamsMixin,
    ModWebChatMixin,
    ModWebTabsMixin,
    ModWebModelsMixin,
    ModWebHomeMixin,
    ModWebStatusMixin,
    ModWebAppPageMixin,
    ModWebEditorsMixin,
    ModWebActionsMixin,
    ModWebUiHelpersMixin,
):
    def __init__(self) -> None:
        self._backend = ModWebDashboardBackend()
        self._startup_lock = asyncio.Lock()
        self._startup_signal = threading.Event()
        self._server_thread: threading.Thread | None = None
        self._startup_error: Exception | None = None
        self._started = False
        self._routes_registered = False
        self._shutting_down = False

    def set_manager(self, manager: App_Manager) -> None:
        self._backend.set_manager(manager)
        app_count = len(manager.apps) if isinstance(getattr(manager, "apps", None), dict) else "unknown"
        log.info("Mod web manager attached: apps=%s", app_count)

    def set_acl(self, acl: Access_Control) -> None:
        self._backend.set_acl(acl)

    def set_relay_tts_service(self, relay_tts_service: RelayTTSQueue | None) -> None:
        self._backend.set_relay_tts_service(relay_tts_service)

    def set_chat_relay_service(self, chat_relay: WebChatRelayPublisher | None) -> None:
        self._backend.set_chat_relay_service(chat_relay)

    @staticmethod
    def _web_display_name(user: ModWebUser) -> str:
        override = config.Name_Cache().get_display_override(user.discord_id, config.DisplayNameCategory.WEB)
        return override or user.display_name

    @staticmethod
    def _web_chat_author_display_name(user: ModWebUser, *, scope: str | None) -> str:
        return config.Name_Cache().relay_display_name(user.discord_id, user.display_name, scope=scope)

    @staticmethod
    def _discord_avatar_uri(user: ModWebUser) -> str | None:
        if user.avatar_hash is None:
            return None
        avatar_hash = user.avatar_hash.strip()
        if not avatar_hash:
            return None
        return f"https://cdn.discordapp.com/avatars/{user.discord_id}/{quote(avatar_hash, safe='')}.png?size=128"

    @staticmethod
    def _user_avatar_markup(*, avatar_uri: str, display_name: str) -> str:
        avatar_alt = escape(f"{display_name} avatar", quote=True)
        return (
            "<img"
            f' class="mod-user-avatar" src="{escape(avatar_uri, quote=True)}"'
            f' alt="{avatar_alt}" loading="lazy" referrerpolicy="no-referrer">'
        )

    def _user_avatar_uri(self, user: ModWebUser) -> str:
        discord_avatar_uri = self._discord_avatar_uri(user)
        if discord_avatar_uri is not None:
            return discord_avatar_uri
        level = self._user_level(user)
        resource_avatar_uri = mod_web_avatars._user_avatar_icon_data_uri(level)
        if resource_avatar_uri is not None:
            return resource_avatar_uri
        return mod_web_avatars._user_avatar_fallback_svg_data_uri(level)

    def app_url(self, app_name: str) -> str:
        return f"{config.MOD_WEB_SERVER.public_base_url}/mod-web/mods/{quote(app_name, safe='')}"

    def app_path(self, app_name: str) -> str:
        return f"/mod-web/mods/{quote(app_name, safe='')}"

    def app_chat_url(self, app_name: str) -> str:
        return f"{config.MOD_WEB_SERVER.public_base_url}{self.app_chat_path(app_name)}"

    def app_chat_path(self, app_name: str) -> str:
        return f"/mod-web/chat/{quote(app_name, safe='')}"

    def node_app_path(self, node_name: str, app_name: str) -> str:
        return f"/mod-web/nodes/{quote(node_name, safe='')}/mods/{quote(app_name, safe='')}"

    def node_app_chat_path(self, node_name: str, app_name: str) -> str:
        return f"/mod-web/nodes/{quote(node_name, safe='')}/chat/{quote(app_name, safe='')}"

    def index_url(self) -> str:
        return config.MOD_WEB_SERVER.public_base_url

    def index_path(self) -> str:
        return "/"

    async def start(self, manager: App_Manager | None = None, acl: Access_Control | None = None) -> None:
        if manager is not None:
            self.set_manager(manager)
        if acl is not None:
            self.set_acl(acl)
        self._shutting_down = False
        await self._ensure_started()

    def begin_shutdown(self) -> None:
        self._shutting_down = True
        self._backend.begin_shutdown()

    async def open_mod_page(self, app: App) -> str:
        await self._ensure_started()
        return self.app_url(app.name)

    async def _ensure_started(self) -> None:
        if self._started:
            return

        async with self._startup_lock:
            if self._started:
                return

            nicegui_app, ui = self._import_nicegui()
            if not self._routes_registered:
                self._register_routes(nicegui_app=nicegui_app, ui=ui)
                self._routes_registered = True

            self._startup_signal.clear()
            self._startup_error = None
            self._server_thread = threading.Thread(
                target=self._run_server,
                args=(nicegui_app, ui),
                name="mod-web",
                daemon=True,
            )
            self._server_thread.start()

            started = await asyncio.to_thread(self._startup_signal.wait, _MOD_WEB_STARTUP_TIMEOUT_SECONDS)
            if not started:
                raise TimeoutError("Timed out while starting the mod web server.")
            if self._startup_error is not None:
                raise RuntimeError(f"Mod web server failed to start: {self._startup_error}") from self._startup_error
            self._started = True
            log.info(
                "Mod web server started: bind=%s:%s public=%s node_api=%s",
                config.MOD_WEB_SERVER.host,
                config.MOD_WEB_SERVER.port,
                config.MOD_WEB_SERVER.public_base_url,
                config.MOD_WEB_SERVER.node_api_base_url,
            )
            log.info(
                "Mod web auth config: enabled=%s bypass=%s redirect=%s",
                self._auth.enabled,
                self._auth.bypass_enabled,
                self._auth.redirect_url,
            )

    def _run_server(self, nicegui_app: object, ui: ModWebRunnerUi) -> None:
        del nicegui_app
        try:
            log.info("Starting mod web server: bind=%s:%s", config.MOD_WEB_SERVER.host, config.MOD_WEB_SERVER.port)
            ui.run(
                host=config.MOD_WEB_SERVER.host,
                port=config.MOD_WEB_SERVER.port,
                log_config=None,
                show=False,
                reload=False,
                title="Yukibot Dashboard",
            )
        except Exception as xcp:
            self._startup_error = xcp
            log.exception("Mod web server failed")
            self._startup_signal.set()

    def _import_nicegui(self) -> tuple[ModWebFastApiApp, ModWebRouteUi]:
        try:
            from nicegui import app as nicegui_app
            from nicegui import ui
        except ImportError as xcp:
            raise RuntimeError("NiceGUI is not installed. Run `uv sync` to install web UI dependencies.") from xcp
        return cast(ModWebFastApiApp, cast(object, nicegui_app)), _cast_mod_web_route_ui(ui)
