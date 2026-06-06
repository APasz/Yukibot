# pyright: reportUnusedFunction=false

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .constants import _MOD_WEB_PAGE_PATH, _SAME_ORIGIN_NODE_PROXY_BASE, log, traffic_log
from .nicegui_protocols import ModWebFastApiApp, ModWebRouteUi
from .runtime_imports import (
    Access_Control,
    Awaitable,
    Callable,
    ModWebAuthError,
    ModWebUser,
    NodeApiScope,
    Power_Level,
    RedirectResponse,
    Request,
    StarletteResponse,
    asyncio,
    config,
    quote,
    requests,
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

class ModWebRoutesMixin(ModWebServiceSupport):
    def _register_routes(self, *, nicegui_app: ModWebFastApiApp, ui: ModWebRouteUi) -> None:
        @nicegui_app.middleware("http")
        async def _log_mod_web_request(
            request: Request,
            call_next: Callable[[Request], Awaitable[StarletteResponse]],
        ) -> StarletteResponse | RedirectResponse:
            redirect = self._remote_portal_redirect(request)
            if redirect is not None:
                traffic_log.info(
                    "Remote mod web redirect: method=%s path=%s target=%s",
                    request.method,
                    request.url.path,
                    redirect.headers.get("location", ""),
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

        @nicegui_app.exception_handler(Exception)
        async def _framework_http_exception(request: Request, exception: Exception) -> object:
            if request.scope.get("nicegui_page_path") and self._should_render_framework_error_page(
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
        self._backend.register_node_api_routes(nicegui_app)

        @nicegui_app.get("/auth/login")
        def _login(request: Request, next_path: str | None = None) -> RedirectResponse:
            del request
            return self._auth.login_redirect(next_path=next_path or self.index_path())

        @nicegui_app.get("/auth/dev-login")
        def _dev_login(level: str, next_path: str | None = None) -> RedirectResponse:
            if not self._auth.bypass_enabled:
                raise _http_exception(404, "Dev login is not available.")
            dev_level = Access_Control.parse_level(level)
            if dev_level is None:
                raise _http_exception(400, f"Unknown dev login level: {level}")
            return self._auth.dev_login_response(level=dev_level, next_path=next_path or self.index_path())

        @nicegui_app.get("/auth/discord/callback")
        async def _discord_callback(
            request: Request, code: str | None = None, state: str | None = None
        ) -> RedirectResponse:
            try:
                return await self._auth.callback_response(request=request, code=code, state=state)
            except ModWebAuthError as xcp:
                log.warning("Mod web Discord OAuth callback rejected: %s", xcp)
                raise _http_exception(400, str(xcp)) from xcp

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
            actions: tuple[tuple[str, str, str], ...],
        ) -> None:
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
                                    on_click=lambda preview_message=message, preview_tone=tone: ui.notify(
                                        preview_message,
                                        type=preview_tone,
                                    ),
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
            content, media_type, headers = await self._remote_bytes_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/map/players",
                scopes=(NodeApiScope.MAP_READ,),
                user=user,
            )
            return StarletteResponse(content=content, media_type=media_type, headers=dict(headers))

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
            content, media_type, headers = await self._remote_bytes_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/map/worlds/{quote(world_name, safe='')}/settings",
                scopes=(NodeApiScope.MAP_READ,),
                user=user,
            )
            return StarletteResponse(content=content, media_type=media_type, headers=dict(headers))

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
            content, media_type, headers = await self._remote_bytes_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/map/worlds/{quote(world_name, safe='')}/markers",
                scopes=(NodeApiScope.MAP_READ,),
                user=user,
            )
            return StarletteResponse(content=content, media_type=media_type, headers=dict(headers))

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
            content, media_type, headers = await self._remote_bytes_async(
                node=node,
                app_name=app_name,
                path=(
                    f"/apps/{quote(app_name, safe='')}/map/worlds/{quote(world_name, safe='')}/tiles/"
                    f"{z}/{quote(tile_name, safe='')}"
                ),
                scopes=(NodeApiScope.MAP_READ,),
                user=user,
            )
            return StarletteResponse(content=content, media_type=media_type, headers=dict(headers))

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/map/assets/{{asset_path:path}}")
        async def _proxy_map_asset(
            node_name: str,
            app_name: str,
            asset_path: str,
            request: Request,
        ) -> StarletteResponse:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            content, media_type, headers = await self._remote_bytes_async(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/map/assets/{quote(asset_path, safe='/')}",
                scopes=(NodeApiScope.MAP_READ,),
                user=user,
            )
            return StarletteResponse(content=content, media_type=media_type, headers=dict(headers))

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/mods")
        async def _proxy_mods(node_name: str, app_name: str, request: Request) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            mods = await asyncio.to_thread(self._remote_mod_list, node, app_name, user)
            return mods.to_mapping()

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/mods/download")
        def _proxy_mods_download(
            node_name: str,
            app_name: str,
            request: Request,
            enabled_only: bool = False,
            selected_only: bool = False,
        ) -> RedirectResponse:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            mod_names = tuple(request.query_params.getlist("mod_name"))
            query = self._download_query(enabled_only=enabled_only, selected_only=selected_only, mod_names=mod_names)
            return self._remote_download_redirect(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/mods/download",
                query=query,
                user=user,
            )

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/configs")
        async def _proxy_configs(node_name: str, app_name: str, request: Request) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            app_entry = await self._remote_app_entry_async(node, app_name, user)
            self._require_user_level(user=user, required_level=app_entry.config_read_level)
            configs = await asyncio.to_thread(self._remote_config_list, node, app_name, user)
            return configs.to_mapping()

        @nicegui_app.get(
            f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/configs/roots/{{root_id}}/download"
        )
        def _proxy_config_root_download(
            node_name: str,
            app_name: str,
            root_id: str,
            request: Request,
        ) -> RedirectResponse:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            return self._remote_download_redirect(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/configs/roots/{quote(root_id, safe='')}/download",
                query={},
                user=user,
                scopes=(NodeApiScope.CONFIGS_READ,),
            )

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/saves")
        async def _proxy_saves(node_name: str, app_name: str, request: Request) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.user)
            node = self._remote_node_link(node_name)
            saves = await asyncio.to_thread(self._remote_save_list, node, app_name, user)
            return saves.to_mapping()

        @nicegui_app.get(
            f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/saves/{{save_id:path}}/download"
        )
        def _proxy_save_download(
            node_name: str,
            app_name: str,
            save_id: str,
            request: Request,
        ) -> RedirectResponse:
            user = self._require_http_user(request=request, required_level=Power_Level.user)
            node = self._remote_node_link(node_name)
            return self._remote_download_redirect(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/saves/{quote(save_id, safe='/')}/download",
                query={},
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
            content = await asyncio.to_thread(self._remote_config_content, node, app_name, config_id, user)
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
            app_entry = await self._remote_app_entry_async(node, app_name, user)
            self._require_user_level(user=user, required_level=app_entry.config_write_level)
            content = payload.get("content")
            if not isinstance(content, str):
                raise _http_exception(400, "Config content is invalid.")
            updated = await asyncio.to_thread(self._remote_config_write, node, app_name, config_id, content, user)
            return updated.to_mapping()

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/settings")
        async def _proxy_settings(node_name: str, app_name: str, request: Request) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.user)
            node = self._remote_node_link(node_name)
            settings = await asyncio.to_thread(self._remote_setting_list, node, app_name, user)
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
            updated = await asyncio.to_thread(self._remote_setting_write, node, app_name, setting_key, value, user)
            return updated.to_mapping()

        @nicegui_app.post(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/settings/save")
        async def _proxy_settings_save(node_name: str, app_name: str, request: Request) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.user)
            node = self._remote_node_link(node_name)
            result = await asyncio.to_thread(self._remote_settings_save, node, app_name, user)
            return result.to_mapping()

        @nicegui_app.post(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/settings/reload")
        async def _proxy_settings_reload(node_name: str, app_name: str, request: Request) -> dict[str, object]:
            user = self._require_http_user(request=request, required_level=Power_Level.user)
            node = self._remote_node_link(node_name)
            result = await asyncio.to_thread(self._remote_settings_reload, node, app_name, user)
            return result.to_mapping()

        @nicegui_app.get(f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{{node_name}}/apps/{{app_name}}/mods/{{mod_name}}/download")
        def _proxy_mod_download(node_name: str, app_name: str, mod_name: str, request: Request) -> RedirectResponse:
            user = self._require_http_user(request=request, required_level=Power_Level.visitor)
            node = self._remote_node_link(node_name)
            return self._remote_download_redirect(
                node=node,
                app_name=app_name,
                path=f"/apps/{quote(app_name, safe='')}/mods/{quote(mod_name, safe='')}/download",
                query={},
                user=user,
            )

        @ui.page("/")
        async def _home_page(request: Request) -> None:
            traffic_log.info("Rendering mod web home page")
            user = self._authorised_page_user(ui=ui, request=request, required_level=Power_Level.visitor)
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
            user = self._authorised_page_user(ui=ui, request=request, required_level=Power_Level.visitor)
            if user is not None:
                await self._render_home_page(
                    ui=ui,
                    user=user,
                    request=request,
                    show_api_actions=self._app_list_api_actions_enabled(request),
                )

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

        @ui.page("/mod-web/nodes/{node_name}")
        async def _node_page(node_name: str, request: Request) -> None:
            traffic_log.info("Rendering mod web node page: node=%s", node_name)
            await self._render_node_page(ui=ui, node_name=node_name, request=request)

        @ui.page("/mod-web/nodes/{node_name}/mods/{app_name}")
        async def _node_mods_page(node_name: str, app_name: str, request: Request) -> None:
            traffic_log.info("Rendering mod web node mods page: node=%s app=%s", node_name, app_name)
            await self._render_node_mods_page(ui=ui, node_name=node_name, app_name=app_name, request=request)

        @ui.page("/mod-web/nodes/{node_name}/chat/{app_name}")
        async def _node_chat_page(node_name: str, app_name: str, request: Request) -> None:
            traffic_log.info("Rendering mod web node chat page: node=%s app=%s", node_name, app_name)
            await self._render_remote_chat_page(ui=ui, node_name=node_name, app_name=app_name, request=request)
