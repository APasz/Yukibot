from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import cast

from aiohttp import web
from aiohttp.web_app import Application

import config
from _authority import AuthorityResource, read_json_object, write_json_object
from config import BotConfiguration, BotMetadataSnapshot, Name_Cache

log = logging.getLogger(__name__)

RequestHandler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _json_object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise web.HTTPBadRequest(text=f"{label} must be a JSON object")
    mapping = cast(Mapping[object, object], value)
    result: dict[str, object] = {}
    for key, item in mapping.items():
        if not isinstance(key, str):
            raise web.HTTPBadRequest(text=f"{label} keys must be strings")
        result[key] = item
    return result


async def _request_json_object(request: web.Request, *, label: str) -> dict[str, object]:
    payload = cast(object, await request.json())
    return _json_object(payload, label=label)


class AuthorityServer:
    _BOT_CONFIGURATION_PATH: Path = Path("configuration.json")

    def __init__(self, names: config.Name_Cache) -> None:
        self._names: Name_Cache = names
        self._bot_configuration_path: Path = self._BOT_CONFIGURATION_PATH
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

        app: Application = web.Application(middlewares=[self._auth_middleware])
        _ = app.router.add_get(path="/authority/bots", handler=self._handle_bots)
        _ = app.router.add_post(path="/authority/bots/sync", handler=self._handle_bots_sync)
        _ = app.router.add_get(path="/authority/users", handler=self._handle_users)
        _ = app.router.add_post(path="/authority/users/replace", handler=self._handle_users_replace)
        _ = app.router.add_get(path="/authority/names", handler=self._handle_names)
        _ = app.router.add_post(path="/authority/names/mutate", handler=self._handle_names_mutate)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(
            runner=self._runner,
            host=config.DATA_AUTHORITY_SERVER_BINDING.host,
            port=config.DATA_AUTHORITY_SERVER_BINDING.port,
        )
        await self._site.start()
        log.info(
            "Authority server listening on %s:%s (public endpoint %s)",
            config.DATA_AUTHORITY_SERVER_BINDING.host,
            config.DATA_AUTHORITY_SERVER_BINDING.port,
            config.DATA_AUTHORITY_ENDPOINT.base_url,
        )
        return True

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler: RequestHandler) -> web.StreamResponse:
        expected: str = f"Bearer {config.DATA_AUTHORITY_TOKEN}"
        if request.headers.get("Authorization") != expected:
            raise web.HTTPUnauthorized()
        return await handler(request)

    async def _handle_users(self, request: web.Request) -> web.Response:
        del request
        data: dict[str, object] = read_json_object(path=config.FILE_USERS)
        return web.json_response(data={"data": data})

    async def _handle_bots(self, request: web.Request) -> web.Response:
        del request
        bot_config: BotConfiguration = config.load_bot_configuration(path=self._bot_configuration_path)
        data: dict[str, object] = {
            bot_id: snapshot.model_dump(mode="json") for bot_id, snapshot in bot_config.known_bots.items()
        }
        write_json_object(path=config.authority_cache_path(resource=AuthorityResource.BOTS), payload=data)
        return web.json_response(data={"data": data})

    async def _handle_bots_sync(self, request: web.Request) -> web.Response:
        payload = await _request_json_object(request, label="payload")

        data = _json_object(payload.get("data"), label="payload.data")

        try:
            snapshot: BotMetadataSnapshot = config.BotMetadataSnapshot.model_validate(data)
        except ValueError as xcp:
            raise web.HTTPBadRequest(text=str(xcp)) from xcp

        config.upsert_known_bot_snapshot(path=self._bot_configuration_path, snapshot=snapshot)
        return web.json_response(data={"ok": True, "data": snapshot.model_dump(mode="json")})

    async def _handle_users_replace(self, request: web.Request) -> web.Response:
        payload = await _request_json_object(request, label="payload")

        data = _json_object(payload.get("data"), label="payload.data")

        write_json_object(path=config.FILE_USERS, payload=data)
        return web.json_response(data={"ok": True, "data": data})

    async def _handle_names(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(data={"data": self._names.serializable()})

    async def _handle_names_mutate(self, request: web.Request) -> web.Response:
        payload = await _request_json_object(request, label="payload")
        event = _json_object(payload.get("event"), label="payload.event")

        try:
            changed: bool = self._names.apply_mutation_event(event)
        except (TypeError, ValueError, KeyError) as xcp:
            raise web.HTTPBadRequest(text=str(xcp)) from xcp
        return web.json_response(data={"ok": True, "changed": changed})
