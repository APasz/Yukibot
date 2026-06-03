from __future__ import annotations

from .constants import log
from .nicegui_protocols import ModWebFastApiApp, WebChatRelayPublisher
from .runtime_imports import (
    Access_Control,
    App_Manager,
    ManagedApp,
    ModWebAuthService,
    NodeApiService,
    Power_Level,
    RelayTTSQueue,
)
from .utils import _http_exception


class ModWebDashboardBackend:
    def __init__(
        self,
        *,
        auth: ModWebAuthService | None = None,
        node_api: NodeApiService | None = None,
    ) -> None:
        self._manager: App_Manager | None = None
        self._acl: Access_Control | None = None
        self._auth: ModWebAuthService = auth or ModWebAuthService()
        self._node_api: NodeApiService = node_api or NodeApiService()
        self._chat_relay: WebChatRelayPublisher | None = None
        self._node_api.set_web_auth(self._auth)

    @property
    def manager(self) -> App_Manager | None:
        return self._manager

    @property
    def acl(self) -> Access_Control | None:
        return self._acl

    @property
    def auth(self) -> ModWebAuthService:
        return self._auth

    @property
    def node_api(self) -> NodeApiService:
        return self._node_api

    @property
    def chat_relay(self) -> WebChatRelayPublisher | None:
        return self._chat_relay

    def set_manager(self, manager: App_Manager) -> None:
        self._manager = manager
        self._node_api.set_manager(manager)

    def replace_manager(self, manager: App_Manager | None) -> None:
        self._manager = manager
        if manager is not None:
            self._node_api.set_manager(manager)

    def set_acl(self, acl: Access_Control) -> None:
        self._acl = acl
        self._node_api.set_acl(acl)

    def replace_acl(self, acl: Access_Control | None) -> None:
        self._acl = acl
        if acl is not None:
            self._node_api.set_acl(acl)

    def set_relay_tts_service(self, relay_tts_service: RelayTTSQueue | None) -> None:
        self._node_api.set_relay_tts_service(relay_tts_service)

    def set_chat_relay_service(self, chat_relay: WebChatRelayPublisher | None) -> None:
        self._chat_relay = chat_relay
        self._node_api.set_chat_relay_service(chat_relay)

    def replace_chat_relay_service(self, chat_relay: WebChatRelayPublisher | None) -> None:
        self.set_chat_relay_service(chat_relay)

    def register_node_api_routes(self, nicegui_app: ModWebFastApiApp) -> None:
        self._node_api.register_routes(nicegui_app)

    def begin_shutdown(self) -> None:
        self._node_api.begin_shutdown()

    def resolve_app(self, app_name: str) -> ManagedApp:
        if self._manager is None:
            log.warning("Mod web app resolve failed because manager is missing: app=%s", app_name)
            raise _http_exception(503, "App manager is not available yet.")
        try:
            log.debug("Mod web resolving app: app=%s", app_name)
            return self._manager.get(app_name)
        except Exception as xcp:
            log.warning("Mod web app not found: app=%s", app_name)
            raise _http_exception(404, f"Unknown app: {app_name}") from xcp

    def managed_apps(self) -> tuple[ManagedApp, ...]:
        if self._manager is None:
            return ()
        return tuple(sorted(self._manager.apps.values(), key=lambda item: item.friendly.casefold()))

    def user_has_level(self, *, user_id: int, required_level: Power_Level) -> bool:
        if self._acl is None:
            return False
        return self._acl.can(user_id, required_level)

    def user_level(self, *, user_id: int) -> Power_Level:
        if self._acl is None:
            return Power_Level.guest
        return self._acl.level_of(user_id)


__all__: tuple[str, ...] = ("ModWebDashboardBackend",)
