from __future__ import annotations

import logging

from aiohttp import web

import config
from _authority import read_json_object, write_json_object

log = logging.getLogger(__name__)


class AuthorityServer:
    def __init__(self, names: config.Name_Cache) -> None:
        self._names = names
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> bool:
        if not config.DATA_AUTHORITY_SERVER_ENABLED:
            return False
        if config.DATA_AUTHORITY_MODE is not config.DataAuthorityMode.LOCAL:
            raise RuntimeError("Authority server can only run in local authority mode")
        if config.DATA_AUTHORITY_ENDPOINT is None:
            raise ValueError("Authority server endpoint is not configured")
        if config.DATA_AUTHORITY_SERVER_BINDING is None:
            raise ValueError("Authority server binding is not configured")
        if not config.DATA_AUTHORITY_TOKEN:
            raise ValueError("DATA_AUTHORITY_TOKEN must be set to run the authority server")

        app = web.Application(middlewares=[self._auth_middleware])
        app.router.add_get("/authority/users", self._handle_users)
        app.router.add_post("/authority/users/replace", self._handle_users_replace)
        app.router.add_get("/authority/names", self._handle_names)
        app.router.add_post("/authority/names/mutate", self._handle_names_mutate)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            config.DATA_AUTHORITY_SERVER_BINDING.host,
            config.DATA_AUTHORITY_SERVER_BINDING.port,
        )
        await self._site.start()
        log.info(
            "Authority server listening on "
            f"{config.DATA_AUTHORITY_SERVER_BINDING.host}:{config.DATA_AUTHORITY_SERVER_BINDING.port} "
            f"(public endpoint {config.DATA_AUTHORITY_ENDPOINT.base_url})"
        )
        return True

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        expected = f"Bearer {config.DATA_AUTHORITY_TOKEN}"
        if request.headers.get("Authorization") != expected:
            raise web.HTTPUnauthorized()
        return await handler(request)

    async def _handle_users(self, request: web.Request) -> web.Response:
        del request
        data = read_json_object(config.FILE_USERS)
        return web.json_response({"data": data})

    async def _handle_users_replace(self, request: web.Request) -> web.Response:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="payload must be a JSON object")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="payload.data must be a JSON object")

        write_json_object(config.FILE_USERS, data)
        return web.json_response({"ok": True, "data": data})

    async def _handle_names(self, request: web.Request) -> web.Response:
        del request
        return web.json_response({"data": self._names.serializable()})

    async def _handle_names_mutate(self, request: web.Request) -> web.Response:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="payload must be a JSON object")
        event = payload.get("event")
        if not isinstance(event, dict):
            raise web.HTTPBadRequest(text="payload.event must be a JSON object")

        try:
            changed = self._names.apply_mutation_event(event)
        except (TypeError, ValueError, KeyError) as xcp:
            raise web.HTTPBadRequest(text=str(xcp)) from xcp
        return web.json_response({"ok": True, "changed": changed})
