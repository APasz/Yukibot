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
from .links import (
    mod_web_node_app_path,
    mod_web_node_chat_path,
)
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
    AbstractEventLoop,
    Access_Control,
    App,
    App_Manager,
    Callable,
    ModWebUser,
    NodeAppStateStreamEvent,
    NodeConsoleStdoutSnapshot,
    NodeStateStreamEvent,
    NodeSystemActionHandler,
    RelayTTSQueue,
    aiohttp,
    asyncio,
    cast,
    config,
    escape,
    requests,
    threading,
)
from .status import ModWebStatusMixin
from .stream_broker import (
    ConsoleStreamKey,
    RemoteAppStreamKey,
    RemoteChatStreamKey,
    RemoteNodeStreamKey,
    SharedAsyncStreamBroker,
)
from .streams import ModWebStreamsMixin
from .tabs import ModWebTabsMixin
from .types import RemoteChatBrokerEvent
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
        self._remote_http_session: aiohttp.ClientSession | None = None
        self._remote_http_session_loop: AbstractEventLoop | None = None
        self._remote_sync_http_local = threading.local()
        self._remote_sync_http_sessions: list[requests.Session] = []
        self._remote_sync_http_sessions_lock = threading.Lock()
        self._remote_node_state_broker: SharedAsyncStreamBroker[RemoteNodeStreamKey, NodeStateStreamEvent] = (
            SharedAsyncStreamBroker()
        )
        self._remote_app_state_broker: SharedAsyncStreamBroker[
            RemoteAppStreamKey, NodeAppStateStreamEvent
        ] = SharedAsyncStreamBroker()
        self._remote_chat_broker: SharedAsyncStreamBroker[
            RemoteChatStreamKey, RemoteChatBrokerEvent
        ] = SharedAsyncStreamBroker()
        self._console_stdout_broker: SharedAsyncStreamBroker[
            ConsoleStreamKey, NodeConsoleStdoutSnapshot
        ] = SharedAsyncStreamBroker()

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

    def set_process_restart_handler(self, handler: Callable[[], None]) -> None:
        self._backend.set_process_restart_handler(handler)

    def set_system_action_handler(self, handler: NodeSystemActionHandler) -> None:
        self._backend.set_system_action_handler(handler)

    @staticmethod
    def _web_display_name(user: ModWebUser) -> str:
        return config.Name_Cache().web_display_name(
            user.discord_id,
            user.display_name,
        )

    @staticmethod
    def _web_chat_author_display_name(
        user: ModWebUser,
        *,
        scope: str | None,
        platforms: tuple[str, ...] = (),
        preferred_platform: str | None = None,
    ) -> str:
        return config.Name_Cache().web_display_name(
            user.discord_id,
            user.display_name,
            scope=scope,
            platforms=platforms,
            preferred_platform=preferred_platform,
        )

    @staticmethod
    def _discord_avatar_uri(user: ModWebUser) -> str | None:
        return mod_web_avatars._discord_avatar_uri(
            user_id=user.discord_id,
            avatar_hash=user.avatar_hash,
        )

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
        return mod_web_avatars._user_avatar_data_uri(level)

    def app_url(self, app_name: str) -> str:
        return f"{config.MOD_WEB_SERVER.public_base_url}{self.app_path(app_name)}"

    def app_path(self, app_name: str) -> str:
        return self.node_app_path(self._default_mod_web_node_name(), app_name)

    def app_chat_url(self, app_name: str) -> str:
        return f"{config.MOD_WEB_SERVER.public_base_url}{self.app_chat_path(app_name)}"

    def app_chat_path(self, app_name: str) -> str:
        return self.node_app_chat_path(self._default_mod_web_node_name(), app_name)

    def node_app_path(self, node_name: str, app_name: str) -> str:
        return mod_web_node_app_path(node_name, app_name)

    def node_app_chat_path(self, node_name: str, app_name: str) -> str:
        return mod_web_node_chat_path(node_name, app_name)

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
