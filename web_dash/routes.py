# pyright: reportUnusedFunction=false

from __future__ import annotations

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import Receive, Scope, Send

from computercraft_mirror import COMPUTERCRAFT_MIRROR_INSTALLER
from mirror_models import MirrorError, MirrorRevisionUnavailable
from font_assets import font_assets
from mod_web_theme import MOD_WEB_THEME_STYLESHEET
from mod_web_toasts import MOD_WEB_TOAST_JAVASCRIPT

from .assets import CacheableTextAsset, cacheable_text_asset
from .constants import (
    _MOD_WEB_PAGE_PATH,
    _PORTAL_HEALTH_PATH,
    _PORTAL_NODE_LATENCIES_PATH,
    _SAME_ORIGIN_NODE_PROXY_BASE,
    log,
    traffic_log,
)
from .nicegui_protocols import ModWebFastApiApp, ModWebNotificationType, ModWebRouteUi
from .runtime_imports import (
    Access_Control,
    Awaitable,
    Callable,
    FileResponse,
    ModWebAuthError,
    ModWebSessionPersistence,
    ModWebUser,
    NodeApiScope,
    PackFormat,
    PackPurpose,
    Path,
    Power_Level,
    RedirectResponse,
    Request,
    StarletteResponse,
    config,
    quote,
    requests,
    urlencode,
    urlsplit,
)
from .service_base import ModWebServiceSupport
from .utils import _http_exception


class _ModWebClientMapErrorReport(BaseModel):
    context: str
    message: str
    stack: str | None = None
    page_path: str | None = None
    app_name: str | None = None
    node_name: str | None = None
    map_api_url: str | None = None
    public_map_url: str | None = None

    model_config = ConfigDict(str_strip_whitespace=False)


class _ModWebGZipMiddleware(GZipMiddleware):
    _EXCLUDED_PATH_PREFIXES: tuple[str, ...] = (
        "/mod-web/assets/fonts/",
    )
    _EXCLUDED_API_PATH_PARTS: tuple[str, ...] = (
        "/download",
        "/map/",
        "/minecraft/recipes/item-icon",
    )

    @classmethod
    def _should_skip_compression(cls, path: str) -> bool:
        if path.startswith(cls._EXCLUDED_PATH_PREFIXES):
            return True
        if not (path.startswith("/api/node/") or path.startswith(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/")):
            return False
        return any(part in path for part in cls._EXCLUDED_API_PATH_PARTS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path")
            if isinstance(path, str) and self._should_skip_compression(path):
                await self.app(scope, receive, send)
                return
        await super().__call__(scope, receive, send)


class ModWebRoutesMixin(ModWebServiceSupport):
    @staticmethod
    def _mod_download_required_level(pack_purpose: PackPurpose | None) -> Power_Level:
        if pack_purpose in {PackPurpose.SERVER, PackPurpose.ADMIN}:
            return Power_Level.sudo
        return Power_Level.visitor

    @staticmethod
    def _static_text_asset_response(
        *,
        request: Request,
        asset: CacheableTextAsset,
    ) -> StarletteResponse:
        cache_headers = {
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{asset.version}"',
            "Vary": "Accept-Encoding",
        }
        if_none_match = request.headers.get("if-none-match")
        validators = () if if_none_match is None else tuple(value.strip() for value in if_none_match.split(","))
        if "*" in validators or cache_headers["ETag"] in validators:
            return StarletteResponse(status_code=304, headers=cache_headers)
        selected = asset.select_content(request.headers.get("accept-encoding"))
        if selected.encoding is not None:
            cache_headers["Content-Encoding"] = selected.encoding.value
        return StarletteResponse(
            content=selected.content,
            media_type=asset.media_type,
            headers=cache_headers,
        )

    @staticmethod
    def _minecraft_item_icon_remote_path(*, app_name: str, item_id: str) -> str:
        return (
            f"/apps/{quote(app_name, safe='')}/minecraft/recipes/item-icon"
            f"?{urlencode({'item_id': item_id})}"
        )

    @staticmethod
    def _is_self_redirect_target(*, request: Request, location: str) -> bool:
        if not location:
            return False
        target = urlsplit(location)
        request_url = getattr(request, "url", None)
        request_path = getattr(request_url, "path", None)
        if not isinstance(request_path, str) or not request_path.startswith("/"):
            return False
        request_query = getattr(request_url, "query", None)
        normalized_request_query = request_query if isinstance(request_query, str) else ""
        if target.scheme or target.netloc:
            request_scheme = getattr(request_url, "scheme", None)
            request_netloc = getattr(request_url, "netloc", None)
            if not isinstance(request_scheme, str) or not isinstance(request_netloc, str):
                return False
            if target.scheme != request_scheme or target.netloc != request_netloc:
                return False
        return (target.path or "/") == request_path and target.query == normalized_request_query

    def _register_routes(self, *, nicegui_app: ModWebFastApiApp, ui: ModWebRouteUi) -> None:
        nicegui_app.add_middleware(_ModWebGZipMiddleware, minimum_size=500, compresslevel=6)

        @nicegui_app.middleware("http")
        async def _log_mod_web_request(
            request: Request,
            call_next: Callable[[Request], Awaitable[StarletteResponse]],
        ) -> StarletteResponse | RedirectResponse:
            redirect = self._remote_portal_redirect(request)
            if redirect is not None:
                redirect_target = redirect.headers.get("location", "")
                if self._is_self_redirect_target(request=request, location=redirect_target):
                    traffic_log.warning(
                        "Remote mod web redirect loop avoided: method=%s path=%s target=%s",
                        request.method,
                        request.url.path,
                        redirect_target,
                    )
                    return await self._build_framework_error_response(
                        ui=ui,
                        request=request,
                        status_code=310,
                        exception=RuntimeError(
                            "Remote mod web attempted to redirect this request back to the same URL."
                        ),
                    )
                traffic_log.info(
                    "Remote mod web redirect: method=%s path=%s target=%s",
                    request.method,
                    request.url.path,
                    redirect_target,
                )
                return redirect

            response = await call_next(request)
            path = request.url.path
            if path == "/" or path.startswith("/mod-web") or path.startswith("/api/node"):
                traffic_log.info(
                    "Mod web request: method=%s path=%s status=%s",
                    request.method,
                    path,
                    response.status_code,
                )
            return response

        previous_404_handler = nicegui_app.exception_handlers.get(404)
        previous_http_exception_handler = nicegui_app.exception_handlers.get(StarletteHTTPException)
        previous_validation_exception_handler = nicegui_app.exception_handlers.get(RequestValidationError)
        previous_exception_handler = nicegui_app.exception_handlers.get(Exception)

        @nicegui_app.exception_handler(404)
        async def _framework_http_404(request: Request, exception: Exception) -> object:
            if self._should_render_framework_error_page(
                method=request.method,
                path=request.url.path,
                accept_header=request.headers.get("accept"),
            ):
                return await self._build_framework_error_response(
                    ui=ui,
                    request=request,
                    status_code=404,
                    exception=exception,
                )
            if previous_404_handler is None:
                raise exception
            return await self._resolve_exception_handler_result(previous_404_handler(request, exception))

        @nicegui_app.exception_handler(StarletteHTTPException)
        async def _framework_http_error(
            request: Request,
            exception: StarletteHTTPException,
        ) -> object:
            if self._should_render_framework_error_page(
                method=request.method,
                path=request.url.path,
                accept_header=request.headers.get("accept"),
            ):
                return await self._build_framework_error_response(
                    ui=ui,
                    request=request,
                    status_code=exception.status_code,
                    exception=exception,
                )
            if previous_http_exception_handler is None:
                raise exception
            return await self._resolve_exception_handler_result(
                previous_http_exception_handler(request, exception)
            )

        @nicegui_app.exception_handler(RequestValidationError)
        async def _framework_request_validation_error(
            request: Request,
            exception: RequestValidationError,
        ) -> object:
            if self._should_render_framework_error_page(
                method=request.method,
                path=request.url.path,
                accept_header=request.headers.get("accept"),
            ):
                return await self._build_framework_error_response(
                    ui=ui,
                    request=request,
                    status_code=422,
                    exception=exception,
                )
            if previous_validation_exception_handler is None:
                raise exception
            return await self._resolve_exception_handler_result(
                previous_validation_exception_handler(request, exception)
            )

        @nicegui_app.exception_handler(Exception)
        async def _framework_http_exception(request: Request, exception: Exception) -> object:
            if self._should_render_framework_error_page(
                method=request.method,
                path=request.url.path,
                accept_header=request.headers.get("accept"),
            ):
                return await self._build_framework_error_response(
                    ui=ui,
                    request=request,
                    status_code=500,
                    exception=exception,
                )
            if previous_exception_handler is None:
                raise exception
            return await self._resolve_exception_handler_result(previous_exception_handler(request, exception))

        def _handle_page_exception(xcp: Exception) -> object:
            return self._render_framework_page_exception(ui=ui, exception=xcp)

        nicegui_app.on_page_exception(_handle_page_exception)
        nicegui_app.on_startup(self._on_startup)
        nicegui_app.on_shutdown(self._on_shutdown)
        self._backend.register_node_api_routes(nicegui_app)

        @nicegui_app.get("/mod-web/assets/theme.css")
        async def _theme_stylesheet(request: Request) -> StarletteResponse:
            return self._static_text_asset_response(
                request=request,
                asset=cacheable_text_asset(MOD_WEB_THEME_STYLESHEET, "text/css"),
            )

        @nicegui_app.get(_PORTAL_HEALTH_PATH)
        async def _portal_health() -> StarletteResponse:
            return StarletteResponse(
                content='{"ok":true}',
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )

        @nicegui_app.get(_PORTAL_NODE_LATENCIES_PATH)
        async def _portal_node_latencies(request: Request) -> dict[str, object]:
            if config.ACTIVE_BOT_PROFILE.name is not config.BotProfileName.PORTAL:
                raise _http_exception(404, "Portal node latency measurements are only available on Portal.")
            self._require_http_user(request=request, required_level=Power_Level.visitor)
            probes = await self._node_api.portal_node_latency_probes_async()
            return {
                "node": config.MOD_WEB_SERVER.node_name,
                "latencies": {node_name: probe.latency_ms for node_name, probe in probes.items()},
                "discord_latencies": {node_name: probe.discord_latency_ms for node_name, probe in probes.items()},
                "discord_service_states": {
                    node_name: probe.discord_service_state.value if probe.discord_service_state is not None else None
                    for node_name, probe in probes.items()
                },
            }

        @nicegui_app.get("/mod-web/assets/toasts.js")
        async def _toast_javascript(request: Request) -> StarletteResponse:
            return self._static_text_asset_response(
                request=request,
                asset=cacheable_text_asset(MOD_WEB_TOAST_JAVASCRIPT, "text/javascript"),
            )

        @nicegui_app.get("/mod-web/assets/map.css")
        async def _map_stylesheet(request: Request) -> StarletteResponse:
            return self._static_text_asset_response(
                request=request,
                asset=cacheable_text_asset(self._map_client_stylesheet(), "text/css"),
            )

        @nicegui_app.get("/mod-web/assets/map.js")
        async def _map_script(request: Request) -> StarletteResponse:
            return self._static_text_asset_response(
                request=request,
                asset=cacheable_text_asset(self._map_client_script(), "text/javascript"),
            )

        @nicegui_app.get("/mod-web/assets/chat.js")
        async def _chat_script(request: Request) -> StarletteResponse:
            return self._static_text_asset_response(
                request=request,
                asset=cacheable_text_asset(self._chat_client_javascript(), "text/javascript"),
            )

        @nicegui_app.get("/mod-web/assets/fonts/{asset_path:path}")
        async def _font_asset(asset_path: str) -> FileResponse:
            requested_path = font_assets.fonts_root / asset_path
            try:
                resolved_path = requested_path.resolve(strict=True)
            except FileNotFoundError as xcp:
                raise _http_exception(404, "Font asset not found.") from xcp
            fonts_root = font_assets.fonts_root.resolve()
            try:
                resolved_path.relative_to(fonts_root)
            except ValueError as xcp:
                raise _http_exception(404, "Font asset not found.") from xcp
            if resolved_path.suffix.casefold() not in {".woff", ".woff2"} or not resolved_path.is_file():
                raise _http_exception(404, "Font asset not found.")
            return FileResponse(
                path=Path(resolved_path),
                filename=resolved_path.name,
                headers={"Cache-Control": "public, max-age=31536000, immutable"},
            )

        @nicegui_app.get("/auth/login")
        def _login(request: Request, next_path: str | None = None, remember: bool = False) -> RedirectResponse:
            del request
            return self._auth.login_redirect(
                next_path=next_path or self.index_path(),
                persistence=ModWebSessionPersistence.from_remembered(remember),
            )

        @nicegui_app.get("/auth/dev-login")
        def _dev_login(level: str, next_path: str | None = None, remember: bool = False) -> RedirectResponse:
            if not self._auth.bypass_enabled:
                raise _http_exception(404, "Dev login is not available.")
            dev_level = Access_Control.parse_level(level)
            if dev_level is None:
                raise _http_exception(400, f"Unknown dev login level: {level}")
            return self._auth.dev_login_response(
                level=dev_level,
                next_path=next_path or self.index_path(),
                persistence=ModWebSessionPersistence.from_remembered(remember),
            )

        @nicegui_app.get("/auth/discord/callback")
        async def _discord_callback(
            request: Request,
            code: str | None = None,
            state: str | None = None,
            error: str | None = None,
        ) -> RedirectResponse:
            try:
                if error == "access_denied":
                    raise ModWebAuthError("Discord sign-in was cancelled.")
                if error is not None:
                    raise ModWebAuthError("Discord did not complete the sign-in request.")
                return await self._auth.callback_response(request=request, code=code, state=state)
            except ModWebAuthError as xcp:
                log.warning("Mod web Discord OAuth callback rejected: %s", xcp)
                error_url = f"/auth/error?{urlencode({'detail': str(xcp)[:500]})}"
                return RedirectResponse(error_url, status_code=303)

        @ui.page("/auth/error")
        async def _oauth_failure(request: Request) -> None:
            detail = request.query_params.get("detail")
            self._render_oauth_failure_page(
                ui=ui,
                detail=detail if isinstance(detail, str) and detail else "Discord sign-in did not complete.",
            )

        @ui.page("/auth/about")
        async def _about(request: Request) -> None:
            del request
            self._render_about_page(ui=ui)

        @nicegui_app.get("/auth/logout")
        def _logout(request: Request) -> RedirectResponse:
            self._auth.logout_request(request)
            return self._auth.logout_response()

        @nicegui_app.post("/mod-web/client-errors/map")
        async def _client_map_error_report(
            payload: _ModWebClientMapErrorReport,
            request: Request,
        ) -> dict[str, bool]:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            log.warning(
                ("Map client error: user=%s(%s) node=%s app=%s context=%s page=%s map_api=%s public_map=%s message=%s"),
                self._web_display_name(user),
                user.discord_id,
                payload.node_name,
                payload.app_name,
                payload.context,
                payload.page_path,
                payload.map_api_url,
                payload.public_map_url,
                payload.message,
            )
            if payload.stack:
                log.warning("Map client error stack:\n%s", payload.stack)
            return {"ok": True}

        def _require_dev_preview_enabled() -> None:
            if not config.INDEV:
                raise _http_exception(404, "Dev error previews are not available.")

        def _dev_preview_user() -> ModWebUser:
            return ModWebUser(
                discord_id=42,
                username="dev_preview",
                global_name="Dev Preview",
                avatar_hash=None,
            )

        def _render_dev_notification_preview_page(
            *,
            title: str,
            support_text: str,
            actions: tuple[tuple[str, str, ModWebNotificationType], ...],
        ) -> None:
            def _notify_action(message: str, tone: ModWebNotificationType) -> Callable[[], None]:
                def notify() -> None:
                    ui.notify(message, type=tone)

                return notify

            self._apply_theme(ui=ui)
            with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
                with ui.card().classes("mod-card w-full"):
                    with ui.column().classes("gap-4 p-5"):
                        with ui.column().classes("gap-1"):
                            ui.label(title).classes("text-xl font-bold mod-title-small")
                            ui.label(support_text).classes("text-sm mod-subtitle")
                        with ui.row().classes("gap-2 flex-wrap"):
                            for label, message, tone in actions:
                                ui.button(
                                    label,
                                    on_click=_notify_action(message, tone),
                                ).classes("mod-list-button")
                        self._action_link(ui=ui, label="Back to Sign In", url=self.index_path(), compact=True)

        @ui.page("/mod-web/dev/error/access-denied")
        async def _dev_access_denied(request: Request) -> None:
            del request
            _require_dev_preview_enabled()
            self._render_forbidden_page(ui=ui, user=_dev_preview_user(), required_level=Power_Level.admin)

        @ui.page("/mod-web/dev/error/sign-in-unavailable")
        async def _dev_sign_in_unavailable(request: Request) -> None:
            del request
            _require_dev_preview_enabled()
            self._render_auth_setup_page(ui=ui)

        @ui.page("/mod-web/dev/error/oauth-failure")
        async def _dev_oauth_failure(request: Request) -> None:
            del request
            _require_dev_preview_enabled()
            self._render_oauth_failure_page(
                ui=ui,
                detail="Discord OAuth state did not match this browser session.",
            )

        @ui.page("/mod-web/dev/error/page-unavailable")
        async def _dev_page_unavailable(request: Request) -> None:
            del request
            _require_dev_preview_enabled()
            self._render_error_page(
                ui=ui,
                title="Page unavailable",
                detail="RuntimeError: This is a dev preview of the app page failure surface.",
                app_name="dev_preview_alpha",
            )

        @ui.page("/mod-web/dev/error/chat-unavailable")
        async def _dev_chat_unavailable(request: Request) -> None:
            del request
            _require_dev_preview_enabled()
            self._render_error_page(
                ui=ui,
                title="Chat unavailable",
                detail="RuntimeError: This is a dev preview of the chat page failure surface.",
                app_name="dev_preview_alpha",
            )

        @ui.page("/mod-web/dev/error/node-unavailable")
        async def _dev_node_unavailable(request: Request) -> None:
            del request
            _require_dev_preview_enabled()
            self._render_remote_node_unavailable_page(
                ui=ui,
                node_name="erin",
                exception=requests.ConnectionError("Dev preview simulated an offline node."),
            )

        @ui.page("/mod-web/dev/error/remote-json-invalid")
        async def _dev_remote_json_invalid(request: Request) -> None:
            del request
            _require_dev_preview_enabled()
            self._render_remote_node_unavailable_page(
                ui=ui,
                node_name="erin",
                exception=RuntimeError("Remote node returned invalid JSON."),
            )

        @ui.page("/mod-web/dev/error/remote-timeout")
        async def _dev_remote_timeout(request: Request) -> None:
            del request
            _require_dev_preview_enabled()
            self._render_remote_node_unavailable_page(
                ui=ui,
                node_name="erin",
                exception=requests.Timeout("Dev preview simulated a slow remote node."),
            )

        @ui.page("/mod-web/dev/error/remote-rejected")
        async def _dev_remote_rejected(request: Request) -> None:
            del request
            _require_dev_preview_enabled()
            self._render_remote_node_unavailable_page(
                ui=ui,
                node_name="erin",
                exception=RuntimeError("Remote node rejected the request: missing scope token."),
            )

        @ui.page("/mod-web/dev/error/redirect-loop")
        async def _dev_redirect_loop(request: Request) -> None:
            del request
            _require_dev_preview_enabled()
            self._apply_theme(ui=ui)
            with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
                self._render_status_page_panel(
                    ui=ui,
                    config=self._framework_http_error_config(
                        status_code=310,
                        exception=RuntimeError(
                            "Remote mod web attempted to redirect this request back to the same URL."
                        ),
                    ),
                )

        @ui.page("/mod-web/dev/error/framework-404")
        async def _dev_framework_404(request: Request) -> None:
            del request
            _require_dev_preview_enabled()
            raise _http_exception(404, "This is a dev preview of the framework 404 page.")

        @ui.page("/mod-web/dev/error/framework-500")
        async def _dev_framework_500(request: Request) -> None:
            del request
            _require_dev_preview_enabled()
            self._render_framework_page_exception(
                ui=ui,
                exception=RuntimeError("This is a dev preview of the framework 500 page."),
            )

        @ui.page("/mod-web/dev/error/nicegui-exception")
        async def _dev_nicegui_exception(request: Request) -> None:
            del request
            _require_dev_preview_enabled()
            raise RuntimeError("This is a dev preview of the NiceGUI page exception surface.")

        @ui.page("/mod-web/dev/error/refresh-shutdown")
        async def _dev_refresh_shutdown(request: Request) -> None:
            del request
            _require_dev_preview_enabled()
            self._apply_theme(ui=ui)
            with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
                self._render_status_page_panel(
                    ui=ui,
                    config=self._framework_http_error_config(
                        status_code=500,
                        exception=RuntimeError("cannot schedule new futures after shutdown"),
                    ),
                )

        @ui.page("/mod-web/dev/error/config-failure")
        async def _dev_config_failure(request: Request) -> None:
            del request
            _require_dev_preview_enabled()
            _render_dev_notification_preview_page(
                title="Config Failure Toasts",
                support_text="Preview the negative notifications used when config loads or saves fail.",
                actions=(
                    ("Preview Load Fail", "Config load failed: RuntimeError: preview load failure", "negative"),
                    ("Preview Save Fail", "Config save failed: RuntimeError: preview save failure", "negative"),
                ),
            )

        @ui.page("/mod-web/dev/error/chat-stream-websocket")
        async def _dev_chat_stream_websocket(request: Request) -> None:
            del request
            _require_dev_preview_enabled()
            self._render_error_page(
                ui=ui,
                title="Chat unavailable",
                detail="RuntimeError: Remote chat stream websocket error: preview disconnect",
                app_name="dev_preview_alpha",
            )

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps")
        async def _proxy_apps(node_name: str, request: Request) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.user)
            node = self._remote_node_link(node_name)
            apps = await self._remote_apps_async(node, user)
            return {"node": node.node_name, "apps": [entry.to_mapping() for entry in apps]}

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/map/manifest")
        async def _proxy_map_manifest(node_name: str, app_name: str, request: Request) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            return await self._remote_json_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/map/manifest",
                scopes=(NodeApiScope.MAP_READ,),
                user=user,
            )

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/map/annotations")
        async def _proxy_map_annotations(node_name: str, app_name: str, request: Request) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            return await self._remote_json_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/map/annotations",
                scopes=(NodeApiScope.MAP_READ,),
                user=user,
            )

        @nicegui_app.post(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/map/annotations")
        async def _proxy_create_map_annotation(
            node_name: str,
            app_name: str,
            payload: dict[str, object],
            request: Request,
        ) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.user)
            node = self._remote_node_link(node_name)
            return await self._remote_json_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/map/annotations",
                scopes=(NodeApiScope.MAP_WRITE,),
                user=user,
                method="POST",
                json_payload=payload,
            )

        @nicegui_app.post(
            f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/map/annotations/{{annotation_id}}/delete"
        )
        async def _proxy_delete_map_annotation(
            node_name: str,
            app_name: str,
            annotation_id: str,
            request: Request,
        ) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.user)
            node = self._remote_node_link(node_name)
            return await self._remote_json_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/map/annotations/{quote(annotation_id, safe='')}/delete",
                scopes=(NodeApiScope.MAP_WRITE,),
                user=user,
                method="POST",
                json_payload={},
            )

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/map/players")
        async def _proxy_map_players(node_name: str, app_name: str, request: Request) -> StarletteResponse:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            return await self._remote_stream_response_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/map/players",
                scopes=(NodeApiScope.MAP_READ,),
                user=user,
            )

        @nicegui_app.get(
            f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/map/worlds/{{world_name}}/settings"
        )
        async def _proxy_map_world_settings(
            node_name: str,
            app_name: str,
            world_name: str,
            request: Request,
        ) -> StarletteResponse:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            return await self._remote_stream_response_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/map/worlds/{quote(world_name, safe='')}/settings",
                scopes=(NodeApiScope.MAP_READ,),
                user=user,
            )

        @nicegui_app.get(
            f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/map/worlds/{{world_name}}/markers"
        )
        async def _proxy_map_world_markers(
            node_name: str,
            app_name: str,
            world_name: str,
            request: Request,
        ) -> StarletteResponse:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            return await self._remote_stream_response_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/map/worlds/{quote(world_name, safe='')}/markers",
                scopes=(NodeApiScope.MAP_READ,),
                user=user,
            )

        @nicegui_app.get(
            f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/map/worlds/{{world_name}}/tiles/{{z}}/{{tile_name}}"
        )
        async def _proxy_map_world_tile(
            node_name: str,
            app_name: str,
            world_name: str,
            z: int,
            tile_name: str,
            request: Request,
        ) -> StarletteResponse:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            return await self._remote_stream_response_async(
                node=node,
                app_name=app_name,
                path=(
                    f"/apps/{quote(app_name, safe='')}/map/worlds/{quote(world_name, safe='')}/tiles/"
                    f"{z}/{quote(tile_name, safe='')}"
                ),
                scopes=(NodeApiScope.MAP_READ,),
                user=user,
            )

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/map/assets/{{asset_path:path}}")
        async def _proxy_map_asset(
            node_name: str,
            app_name: str,
            asset_path: str,
            request: Request,
        ) -> StarletteResponse:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            return await self._remote_stream_response_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/map/assets/{quote(asset_path, safe='/')}",
                scopes=(NodeApiScope.MAP_READ,),
                user=user,
            )

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/minecraft/recipes/item-icon")
        async def _proxy_minecraft_recipe_item_icon(
            node_name: str,
            app_name: str,
            item_id: str,
            request: Request,
        ) -> StarletteResponse:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            try:
                return await self._remote_stream_response_async(
                    node=node,
                    app_name=app_name,
                    path=self._minecraft_item_icon_remote_path(app_name=app_name, item_id=item_id),
                    scopes=(NodeApiScope.MODS_READ,),
                    user=user,
                    timeout=60.0,
                )
            except Exception as xcp:
                log.warning(
                    "Minecraft item icon proxy failed: node=%s app=%s item=%s error=%s",
                    node_name,
                    app_name,
                    item_id,
                    xcp,
                )
                return StarletteResponse(
                    content=self._node_api.minecraft_item_icon_placeholder_svg(item_id),
                    media_type="image/svg+xml",
                    headers={"Cache-Control": "private, max-age=30"},
                )

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/mods")
        async def _proxy_mods(node_name: str, app_name: str, request: Request) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            mods = await self._remote_mod_list_async(node, app_name, user)
            return mods.to_mapping()

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/mods/download")
        async def _proxy_mods_download(
            node_name: str,
            app_name: str,
            request: Request,
            enabled_only: bool = False,
            selected_only: bool = False,
            excluded_only: bool = False,
            client_pack: bool = False,
            pack_purpose: PackPurpose | None = None,
            pack_format: PackFormat = PackFormat.GENERIC_ZIP,
            publish_client_pack: bool = False,
        ) -> StarletteResponse:
            user = self._require_http_user(
                request=request,
                required_level=(
                    Power_Level.user if publish_client_pack else self._mod_download_required_level(pack_purpose)
                ),
            )
            node = self._remote_node_link(node_name)
            mod_names = tuple(request.query_params.getlist("mod_name"))
            query = self._download_query(
                enabled_only=enabled_only,
                selected_only=selected_only,
                excluded_only=excluded_only,
                mod_names=mod_names,
                client_pack=client_pack,
                pack_purpose=pack_purpose,
                pack_format=pack_format,
                publish_client_pack=publish_client_pack,
            )
            scopes = (NodeApiScope.MODS_DOWNLOAD,)
            if pack_purpose in {PackPurpose.SERVER, PackPurpose.ADMIN} or publish_client_pack:
                scopes = (NodeApiScope.MODS_DOWNLOAD, NodeApiScope.MODS_WRITE)
            return await self._remote_stream_response_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/mods/download",
                query=query,
                user=user,
                scopes=scopes,
            )

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/configs")
        async def _proxy_configs(node_name: str, app_name: str, request: Request) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            app_entry = await self._remote_app_entry_async(node, app_name, user)
            self._require_user_level(user=user, required_level=app_entry.config_read_level)
            configs = await self._remote_config_list_async(node, app_name, user)
            return configs.to_mapping()

        @nicegui_app.get(
            f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/configs/roots/{{root_id}}/download"
        )
        async def _proxy_config_root_download(
            node_name: str,
            app_name: str,
            root_id: str,
            request: Request,
        ) -> StarletteResponse:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            return await self._remote_stream_response_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/configs/roots/{quote(root_id, safe='')}/download",
                user=user,
                scopes=(NodeApiScope.CONFIGS_READ,),
            )

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/saves")
        async def _proxy_saves(node_name: str, app_name: str, request: Request) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.user)
            node = self._remote_node_link(node_name)
            saves = await self._remote_save_list_async(node, app_name, user)
            return saves.to_mapping()

        @nicegui_app.get(
            f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/saves/{{save_id:path}}/download"
        )
        async def _proxy_save_download(
            node_name: str,
            app_name: str,
            save_id: str,
            request: Request,
        ) -> StarletteResponse:
            user = self._require_http_user(request=request, required_level=Power_Level.user)
            node = self._remote_node_link(node_name)
            return await self._remote_stream_response_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/saves/{quote(save_id, safe='/')}/download",
                user=user,
                scopes=(NodeApiScope.SAVES_DOWNLOAD,),
            )

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/configs/{{config_id:path}}")
        async def _proxy_config_read(
            node_name: str, app_name: str, config_id: str, request: Request
        ) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            app_entry = await self._remote_app_entry_async(node, app_name, user)
            self._require_user_level(user=user, required_level=app_entry.config_read_level)
            content = await self._remote_config_content_async(node, app_name, config_id, user)
            return content.to_mapping()

        @nicegui_app.put(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/configs/{{config_id:path}}")
        async def _proxy_config_write(
            node_name: str,
            app_name: str,
            config_id: str,
            payload: dict[str, object],
            request: Request,
        ) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.user)
            node = self._remote_node_link(node_name)
            content = payload.get("content")
            if not isinstance(content, str):
                raise _http_exception(400, "Config content is invalid.")
            updated = await self._remote_config_write_async(node, app_name, config_id, content, user)
            return updated.to_mapping()

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/settings")
        async def _proxy_settings(node_name: str, app_name: str, request: Request) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.user)
            node = self._remote_node_link(node_name)
            settings = await self._remote_setting_list_async(node, app_name, user)
            return settings.to_mapping()

        @nicegui_app.put(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/settings/{{setting_key}}")
        async def _proxy_setting_write(
            node_name: str,
            app_name: str,
            setting_key: str,
            payload: dict[str, object],
            request: Request,
        ) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.user)
            node = self._remote_node_link(node_name)
            value = payload.get("value")
            if not isinstance(value, str):
                raise _http_exception(400, "Setting value is invalid.")
            updated = await self._remote_setting_write_async(node, app_name, setting_key, value, user)
            return updated.to_mapping()

        @nicegui_app.post(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/settings/save")
        async def _proxy_settings_save(node_name: str, app_name: str, request: Request) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.user)
            node = self._remote_node_link(node_name)
            result = await self._remote_settings_save_async(node, app_name, user)
            return result.to_mapping()

        @nicegui_app.post(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/settings/reload")
        async def _proxy_settings_reload(node_name: str, app_name: str, request: Request) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.user)
            node = self._remote_node_link(node_name)
            result = await self._remote_settings_reload_async(node, app_name, user)
            return result.to_mapping()

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/mods/{{mod_name}}/download")
        async def _proxy_mod_download(
            node_name: str,
            app_name: str,
            mod_name: str,
            request: Request,
        ) -> StarletteResponse:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            return await self._remote_stream_response_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/mods/{quote(mod_name, safe='')}/download",
                user=user,
                scopes=(NodeApiScope.MODS_DOWNLOAD,),
            )

        @nicegui_app.get(
            f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/factorio/mod-settings/download"
        )
        async def _proxy_factorio_mod_settings_download(
            node_name: str,
            app_name: str,
            request: Request,
        ) -> StarletteResponse:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            return await self._remote_stream_response_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/factorio/mod-settings/download",
                user=user,
                scopes=(NodeApiScope.CONFIGS_READ,),
            )

        if config.mirror_hosting_enabled():
            mirrors = self._mirror_service()

            @nicegui_app.get("/mirror/v1/installer.lua")
            def _computercraft_mirror_installer() -> StarletteResponse:
                return StarletteResponse(
                    content=COMPUTERCRAFT_MIRROR_INSTALLER,
                    media_type="text/plain",
                    headers={"Cache-Control": "no-cache"},
                )

            @nicegui_app.get("/mirror/v1/projects/{project_id}/manifest.json")
            def _mirror_manifest(project_id: str) -> FileResponse:
                try:
                    manifest_path = mirrors.manifest_path(project_id)
                except MirrorError as xcp:
                    raise _http_exception(404, "Mirror project was not found.") from xcp
                if manifest_path is None:
                    raise _http_exception(404, "Mirror snapshot was not found.")
                return FileResponse(
                    path=manifest_path,
                    media_type="application/json",
                    headers={"Cache-Control": "no-cache"},
                )

            @nicegui_app.get("/mirror/v1/projects/{project_id}/files/{relative_path:path}")
            def _mirror_file(project_id: str, relative_path: str, revision: str | None = None) -> FileResponse:
                try:
                    file_path = mirrors.file_path(
                        project_id=project_id,
                        relative_path=relative_path,
                        revision=revision,
                    )
                except MirrorRevisionUnavailable as xcp:
                    raise _http_exception(409, "Mirror snapshot changed; fetch a new manifest and retry.") from xcp
                except MirrorError as xcp:
                    raise _http_exception(404, "Mirror project was not found.") from xcp
                if file_path is None:
                    raise _http_exception(404, "Mirror file was not found.")
                cache_control = "public, max-age=31536000, immutable" if revision is not None else "no-cache"
                return FileResponse(
                    path=file_path,
                    media_type="application/octet-stream",
                    headers={"Cache-Control": cache_control},
                )

        @ui.page("/")
        async def _home_page(request: Request) -> None:
            traffic_log.info("Rendering mod web home page")
            user = await self._authorised_page_user(ui=ui, request=request, required_level=Power_Level.visitor)
            if user is not None:
                await self._render_home_page(
                    ui=ui,
                    user=user,
                    request=request,
                    show_api_actions=self._app_list_api_actions_enabled(request),
                )

        @ui.page("/mod-web")
        async def _mod_web_home_page(request: Request) -> None:
            traffic_log.info("Rendering mod web home page: path=/mod-web")
            user = await self._authorised_page_user(ui=ui, request=request, required_level=Power_Level.visitor)
            if user is not None:
                await self._render_home_page(
                    ui=ui,
                    user=user,
                    request=request,
                    show_api_actions=self._app_list_api_actions_enabled(request),
                )

        if config.mirror_hosting_enabled():

            @ui.page("/mod-web/mirrors")
            async def _mirrors_page(request: Request) -> None:
                traffic_log.info("Rendering mirror dashboard")
                user = await self._authorised_page_user(ui=ui, request=request, required_level=Power_Level.user)
                if user is not None:
                    await self._render_mirrors_page(ui=ui, user=user)

        @ui.page("/aliases")
        @ui.page("/mod-web/aliases")
        async def _aliases_page(request: Request) -> None:
            traffic_log.info("Rendering alias page")
            user = await self._authorised_page_user(ui=ui, request=request, required_level=Power_Level.visitor)
            if user is not None:
                await self._render_alias_page(ui=ui, user=user, request=request)

        @ui.page("/app-installer")
        @ui.page("/mod-web/app-installer")
        async def _app_installer_page(request: Request) -> None:
            traffic_log.info("Rendering app installer")
            user = await self._authorised_page_user(ui=ui, request=request, required_level=Power_Level.sudo)
            if user is not None:
                await self._render_app_installer_page(ui=ui, user=user)

        @ui.page("/apps/{app_name}")
        async def _app_alias_page(app_name: str, request: Request) -> None:
            traffic_log.info("Rendering app alias page: app=%s", app_name)
            await self._render_mods_page(ui=ui, app_name=app_name, request=request)

        @ui.page("/app/{app_name}")
        async def _single_app_alias_page(app_name: str, request: Request) -> None:
            traffic_log.info("Rendering app alias page: app=%s", app_name)
            await self._render_mods_page(ui=ui, app_name=app_name, request=request)

        @ui.page("/mods/{app_name}")
        async def _mods_alias_page(app_name: str, request: Request) -> None:
            traffic_log.info("Rendering mods alias page: app=%s", app_name)
            await self._render_mods_page(ui=ui, app_name=app_name, request=request)

        @ui.page("/mod-web/apps/{app_name}")
        async def _mod_web_apps_alias_page(app_name: str, request: Request) -> None:
            traffic_log.info("Rendering mod web apps alias page: app=%s", app_name)
            await self._render_mods_page(ui=ui, app_name=app_name, request=request)

        @ui.page("/chat/{app_name}")
        async def _chat_alias_page(app_name: str, request: Request) -> None:
            traffic_log.info("Rendering chat alias page: app=%s", app_name)
            await self._render_chat_page(ui=ui, app_name=app_name, request=request)

        @ui.page("/mod-web/chat/{app_name}")
        async def _mod_web_chat_page(app_name: str, request: Request) -> None:
            traffic_log.info("Rendering mod web chat page: app=%s", app_name)
            await self._render_chat_page(ui=ui, app_name=app_name, request=request)

        @ui.page(_MOD_WEB_PAGE_PATH)
        async def _mods_page(app_name: str, request: Request) -> None:
            traffic_log.info("Rendering mod web mods page: app=%s", app_name)
            await self._render_mods_page(ui=ui, app_name=app_name, request=request)

        @ui.page("/mod-web/nodes/{node_name}/system")
        async def _node_system_page(node_name: str, request: Request) -> None:
            traffic_log.info("Rendering mod web node system page: node=%s", node_name)
            await self._render_node_system_page(ui=ui, node_name=node_name, request=request)

        @ui.page("/mod-web/nodes/{node_name}/mods/{app_name}")
        async def _node_mods_page(node_name: str, app_name: str, request: Request) -> None:
            traffic_log.info("Rendering mod web node mods page: node=%s app=%s", node_name, app_name)
            await self._render_node_mods_page(ui=ui, node_name=node_name, app_name=app_name, request=request)

        @ui.page("/mod-web/nodes/{node_name}/chat/{app_name}")
        async def _node_chat_page(node_name: str, app_name: str, request: Request) -> None:
            traffic_log.info("Rendering mod web node chat page: node=%s app=%s", node_name, app_name)
            await self._render_remote_chat_page(ui=ui, node_name=node_name, app_name=app_name, request=request)
