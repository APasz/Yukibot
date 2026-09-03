"""Game-specific app operations exposed through the node API."""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi.responses import Response

import apps.minecraft.node_api as minecraft_node_api
import apps.sevendays.node_api as sevendays_node_api
from _security import Access_Control, Power_Level
from apps._app import App
from apps.minecraft import Minecraft, MinecraftRecipeMutation
from apps.minecraft.node_api import (
    NodeMinecraftRecipeMutationAction,
    NodeMinecraftRecipeMutationRequest,
    NodeMinecraftRecipeMutationResult,
    NodeMinecraftRecipeWorkspaceState,
)
from apps.sevendays import SevenDays
from apps.sevendays.node_api import NodeSevenDaysSandboxOptionsState
from .route_contracts import HttpExceptionFactory


class NodeAppGameService:
    """Owns game-specific app data and Minecraft recipe mutations."""

    def __init__(
        self,
        *,
        node_name: Callable[[], str],
        require_acl: Callable[[], Access_Control],
        http_exception: HttpExceptionFactory,
        traffic_log: logging.Logger,
    ) -> None:
        self._node_name = node_name
        self._require_acl = require_acl
        self._http_exception = http_exception
        self._traffic_log = traffic_log

    def build_minecraft_recipe_workspace_state(
        self, app: App
    ) -> NodeMinecraftRecipeWorkspaceState:
        if not isinstance(app, Minecraft):
            raise self._http_exception(
                404, f"App {app.name!r} does not expose Minecraft recipe data."
            )
        return minecraft_node_api.build_minecraft_recipe_workspace_state(app)

    def build_sevendays_sandbox_options_state(
        self, app: App
    ) -> NodeSevenDaysSandboxOptionsState:
        if not isinstance(app, SevenDays):
            raise self._http_exception(
                404, f"App {app.name!r} does not expose 7D2D sandbox options."
            )
        return sevendays_node_api.build_sevendays_sandbox_options_state(app)

    def build_minecraft_item_icon_response(
        self,
        app: App,
        *,
        item_id: str,
    ) -> Response:
        if not isinstance(app, Minecraft):
            raise self._http_exception(
                404, f"App {app.name!r} does not expose Minecraft recipe item icons."
            )
        try:
            return minecraft_node_api.build_minecraft_item_icon_response(
                app, item_id=item_id
            )
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp

    async def append_minecraft_recipe_mutation(
        self,
        *,
        app: App,
        mutation: MinecraftRecipeMutation,
        actor_user_id: int,
    ) -> NodeMinecraftRecipeMutationResult:
        return await self.mutate_minecraft_recipe_book(
            app=app,
            mutation_request=NodeMinecraftRecipeMutationRequest(
                action=NodeMinecraftRecipeMutationAction.ADD,
                mutation=mutation,
            ),
            actor_user_id=actor_user_id,
        )

    async def mutate_minecraft_recipe_book(
        self,
        *,
        app: App,
        mutation_request: NodeMinecraftRecipeMutationRequest,
        actor_user_id: int,
    ) -> NodeMinecraftRecipeMutationResult:
        if not isinstance(app, Minecraft):
            raise self._http_exception(
                404, f"App {app.name!r} does not expose Minecraft recipe data."
            )
        await self._require_acl().perm_check(actor_user_id, Power_Level.sudo)
        try:
            minecraft_node_api.apply_minecraft_recipe_mutation(
                app=app,
                mutation_request=mutation_request,
                actor_user_id=actor_user_id,
            )
        except IndexError as xcp:
            raise self._http_exception(404, str(xcp)) from xcp
        except FileNotFoundError as xcp:
            raise self._http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        except RuntimeError as xcp:
            raise self._http_exception(409, str(xcp)) from xcp
        except Exception as xcp:
            raise self._http_exception(
                500, f"Minecraft recipe mutation failed: {xcp}"
            ) from xcp
        self._traffic_log.info(
            "Node API Minecraft recipe mutation applied: node=%s app=%s actor=%s action=%s index=%s kind=%s",
            self._node_name(),
            app.name,
            actor_user_id,
            mutation_request.action.value,
            mutation_request.mutation_index,
            None
            if mutation_request.mutation is None
            else mutation_request.mutation.to_mapping().get("kind"),
        )
        return NodeMinecraftRecipeMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            message=f"Saved Minecraft recipe change for {app.friendly}.",
            workspace=self.build_minecraft_recipe_workspace_state(app),
        )


__all__: tuple[str, ...] = ("NodeAppGameService",)
