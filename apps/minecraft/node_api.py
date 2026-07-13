from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import assert_never, cast

from fastapi import Response
from fastapi.responses import FileResponse

import config
from apps._node_api import JsonValue, optional_string, required_bool, required_string
from apps.minecraft import (
    Minecraft,
    MinecraftItemRegistrySnapshot,
    MinecraftRecipeBook,
    MinecraftRecipeMutation,
    generated_minecraft_recipe_mutation_id,
    minecraft_recipe_mutation_id,
    minecraft_recipe_mutation_with_id,
)


@dataclass(frozen=True, slots=True)
class NodeMinecraftRecipeBookState:
    data_path: str
    script_path: str
    payload: dict[str, JsonValue] | None = None
    load_error: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeMinecraftRecipeBookState":
        data_path: str = required_string(payload, "data_path")
        script_path: str = required_string(payload, "script_path")
        raw_book_payload: object | None = payload.get("payload")
        if raw_book_payload is not None and not isinstance(raw_book_payload, Mapping):
            raise ValueError("Node Minecraft recipe book payload is invalid.")
        load_error: str | None = optional_string(payload, "load_error")
        return cls(
            data_path=data_path,
            script_path=script_path,
            payload=None if raw_book_payload is None else dict(cast(Mapping[str, JsonValue], raw_book_payload)),
            load_error=load_error,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "data_path": self.data_path,
            "script_path": self.script_path,
            "payload": self.payload,
            "load_error": self.load_error,
        }


@dataclass(frozen=True, slots=True)
class NodeMinecraftItemRegistryState:
    data_path: str
    file_exists: bool
    payload: dict[str, JsonValue] | None = None
    load_error: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeMinecraftItemRegistryState":
        data_path: str = required_string(payload, "data_path")
        file_exists: bool = required_bool(payload, "file_exists")
        raw_registry_payload: object | None = payload.get("payload")
        if raw_registry_payload is not None and not isinstance(raw_registry_payload, Mapping):
            raise ValueError("Node Minecraft item registry payload is invalid.")
        load_error: str | None = optional_string(payload, "load_error")
        return cls(
            data_path=data_path,
            file_exists=file_exists,
            payload=None if raw_registry_payload is None else dict(cast(Mapping[str, JsonValue], raw_registry_payload)),
            load_error=load_error,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "data_path": self.data_path,
            "file_exists": self.file_exists,
            "payload": self.payload,
            "load_error": self.load_error,
        }


@dataclass(frozen=True, slots=True)
class NodeMinecraftRecipeWorkspaceState:
    recipe_book: NodeMinecraftRecipeBookState
    item_registry: NodeMinecraftItemRegistryState

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeMinecraftRecipeWorkspaceState":
        raw_recipe_book: object | None = payload.get("recipe_book")
        raw_item_registry: object | None = payload.get("item_registry")
        if not isinstance(raw_recipe_book, Mapping):
            raise ValueError("Node Minecraft recipe workspace recipe_book is invalid.")
        if not isinstance(raw_item_registry, Mapping):
            raise ValueError("Node Minecraft recipe workspace item_registry is invalid.")
        return cls(
            recipe_book=NodeMinecraftRecipeBookState.from_mapping(cast(Mapping[str, object], raw_recipe_book)),
            item_registry=NodeMinecraftItemRegistryState.from_mapping(cast(Mapping[str, object], raw_item_registry)),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "recipe_book": self.recipe_book.to_mapping(),
            "item_registry": self.item_registry.to_mapping(),
        }


class NodeMinecraftRecipeMutationAction(StrEnum):
    ADD = "add"
    REPLACE = "replace"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class NodeMinecraftRecipeMutationRequest:
    action: NodeMinecraftRecipeMutationAction
    mutation_index: int | None = None
    mutation: MinecraftRecipeMutation | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeMinecraftRecipeMutationRequest":
        try:
            action: NodeMinecraftRecipeMutationAction = NodeMinecraftRecipeMutationAction(
                required_string(payload, "action")
            )
        except ValueError as xcp:
            raise ValueError("Node Minecraft recipe mutation action is invalid.") from xcp
        raw_mutation_index: object | None = payload.get("mutation_index")
        if raw_mutation_index is None:
            mutation_index = None
        elif isinstance(raw_mutation_index, bool) or not isinstance(raw_mutation_index, int):
            raise ValueError("Node Minecraft recipe mutation index must be an integer when provided.")
        elif raw_mutation_index < 0:
            raise ValueError("Node Minecraft recipe mutation index must not be negative.")
        else:
            mutation_index = raw_mutation_index
        raw_mutation: object | None = payload.get("mutation")
        mutation: MinecraftRecipeMutation | None
        if raw_mutation is None:
            mutation = None
        else:
            if not isinstance(raw_mutation, Mapping):
                raise ValueError("Node Minecraft recipe mutation payload is invalid.")
            empty_recipe_book: MinecraftRecipeBook = MinecraftRecipeBook.empty()
            recipe_book: MinecraftRecipeBook = MinecraftRecipeBook.from_mapping(
                {
                    "schema_version": empty_recipe_book.schema_version,
                    "mutations": [dict[str, object](cast(Mapping[str, object], raw_mutation))],
                }
            )
            if len(recipe_book.mutations) != 1:
                raise ValueError("Node Minecraft recipe mutation payload must contain exactly one mutation.")
            mutation = recipe_book.mutations[0]
        if action is NodeMinecraftRecipeMutationAction.ADD:
            if mutation is None:
                raise ValueError("Node Minecraft recipe add requests require a mutation.")
            if mutation_index is not None:
                raise ValueError("Node Minecraft recipe add requests must not include a mutation index.")
        elif action is NodeMinecraftRecipeMutationAction.REPLACE:
            if mutation is None:
                raise ValueError("Node Minecraft recipe replace requests require a mutation.")
            if mutation_index is None:
                raise ValueError("Node Minecraft recipe replace requests require a mutation index.")
        elif action is NodeMinecraftRecipeMutationAction.DELETE:
            if mutation_index is None:
                raise ValueError("Node Minecraft recipe delete requests require a mutation index.")
            if mutation is not None:
                raise ValueError("Node Minecraft recipe delete requests must not include a mutation payload.")
        else:
            assert_never(action)
        return cls(action=action, mutation_index=mutation_index, mutation=mutation)

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {"action": self.action.value}
        if self.mutation_index is not None:
            payload["mutation_index"] = self.mutation_index
        if self.mutation is not None:
            payload["mutation"] = self.mutation.to_mapping()
        return payload


@dataclass(frozen=True, slots=True)
class NodeMinecraftRecipeMutationResult:
    app_name: str
    app_friendly: str
    node: str
    message: str
    workspace: NodeMinecraftRecipeWorkspaceState

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeMinecraftRecipeMutationResult":
        app_name: str = required_string(payload, "app_name")
        app_friendly: str = required_string(payload, "app_friendly")
        node: str = required_string(payload, "node")
        message: str = required_string(payload, "message")
        raw_workspace: object | None = payload.get("workspace")
        if not isinstance(raw_workspace, Mapping):
            raise ValueError("Node Minecraft recipe mutation workspace is invalid.")
        return cls(
            app_name=app_name,
            app_friendly=app_friendly,
            node=node,
            message=message,
            workspace=NodeMinecraftRecipeWorkspaceState.from_mapping(cast(Mapping[str, object], raw_workspace)),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "message": self.message,
            "workspace": self.workspace.to_mapping(),
        }


def build_minecraft_recipe_workspace_state(app: Minecraft) -> NodeMinecraftRecipeWorkspaceState:
    recipe_data_path = ".yukibot/recipes.json"
    recipe_script_path = "kubejs/server_scripts/yuki_recipes.js"
    try:
        recipe_book: MinecraftRecipeBook = app.load_kubejs_recipe_book()
        recipe_book_payload: dict[str, JsonValue] | None = cast(dict[str, JsonValue], recipe_book.to_mapping())
        recipe_book_load_error: str | None = None
    except Exception as xcp:
        recipe_book_payload = None
        recipe_book_load_error = str(xcp) or type(xcp).__name__
    item_registry_data_path = ".yukibot/registries/items.json"
    item_registry_file_exists: bool = app._resolve_existing_yukibot_data_path(
        current_path=app._yukibot_item_registry_path(),
        legacy_path=app._legacy_yukibot_item_registry_path(),
    ).exists()
    try:
        item_registry: MinecraftItemRegistrySnapshot = app.load_kubejs_item_registry()
        item_registry_payload: dict[str, JsonValue] | None = cast(dict[str, JsonValue], item_registry.to_mapping())
        item_registry_load_error: str | None = None
    except Exception as xcp:
        item_registry_payload = None
        item_registry_load_error = str(xcp) or type(xcp).__name__
    return NodeMinecraftRecipeWorkspaceState(
        recipe_book=NodeMinecraftRecipeBookState(
            data_path=recipe_data_path,
            script_path=recipe_script_path,
            payload=recipe_book_payload,
            load_error=recipe_book_load_error,
        ),
        item_registry=NodeMinecraftItemRegistryState(
            data_path=item_registry_data_path,
            file_exists=item_registry_file_exists,
            payload=item_registry_payload,
            load_error=item_registry_load_error,
        ),
    )


def build_minecraft_item_icon_response(app: Minecraft, *, item_id: str) -> Response:
    icon_path: Path | None = app.resolve_minecraft_item_icon_path(item_id)
    if icon_path is not None:
        return FileResponse(
            icon_path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=300"},
        )
    return Response(
        content=minecraft_item_icon_placeholder_svg(item_id),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=60"},
    )


def minecraft_item_icon_placeholder_svg(item_id: str) -> str:
    resource_text: str = item_id.strip().casefold()
    item_tail: str = resource_text.rsplit(":", maxsplit=1)[-1].split("/")[-1]
    condensed_tail: str = "".join(character for character in item_tail if character.isalnum())
    badge_text: str = (condensed_tail[:2] or "??").upper()
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="Minecraft item icon">'
        '<rect width="64" height="64" fill="#0b0b10"/>'
        '<rect x="3" y="3" width="58" height="58" fill="#17171f" stroke="#52525b" stroke-width="2"/>'
        '<rect x="7" y="7" width="50" height="50" fill="#1f1a2b" stroke="#8b5cf6" stroke-opacity="0.42"/>'
        f'<text x="32" y="38" fill="#ede9fe" font-size="18" font-family="monospace" text-anchor="middle">{badge_text}</text>'
        "</svg>"
    )


def apply_minecraft_recipe_mutation(
    *,
    app: Minecraft,
    mutation_request: NodeMinecraftRecipeMutationRequest,
    actor_user_id: int,
) -> None:
    if mutation_request.action is NodeMinecraftRecipeMutationAction.ADD:
        assert mutation_request.mutation is not None
        mutation = minecraft_recipe_mutation_for_actor(
            app=app,
            mutation=mutation_request.mutation,
            mutation_index=None,
            actor_user_id=actor_user_id,
        )
        app.append_kubejs_recipe_mutation(mutation)
    elif mutation_request.action is NodeMinecraftRecipeMutationAction.REPLACE:
        assert mutation_request.mutation_index is not None
        assert mutation_request.mutation is not None
        mutation = minecraft_recipe_mutation_for_actor(
            app=app,
            mutation=mutation_request.mutation,
            mutation_index=mutation_request.mutation_index,
            actor_user_id=actor_user_id,
        )
        app.replace_kubejs_recipe_mutation(mutation_request.mutation_index, mutation)
    elif mutation_request.action is NodeMinecraftRecipeMutationAction.DELETE:
        assert mutation_request.mutation_index is not None
        app.remove_kubejs_recipe_mutation(mutation_request.mutation_index)
    else:
        assert_never(mutation_request.action)


def minecraft_recipe_mutation_for_actor(
    *,
    app: Minecraft,
    mutation: MinecraftRecipeMutation,
    mutation_index: int | None,
    actor_user_id: int,
) -> MinecraftRecipeMutation:
    recipe_book: MinecraftRecipeBook = app.load_kubejs_recipe_book()
    if mutation_index is not None:
        if mutation_index < 0 or mutation_index >= len(recipe_book.mutations):
            raise IndexError(f"Unknown Minecraft recipe mutation index: {mutation_index}")
    minecraft_username: str | None = config.Name_Cache().get_game_alias(actor_user_id, "minecraft")
    if minecraft_username is None:
        raise ValueError(
            "Link a Minecraft username to your Discord account before creating recipes or removal directives."
        )
    existing_recipe_ids: set[str] = {
        recipe_id
        for existing_index, existing_mutation in enumerate[MinecraftRecipeMutation](recipe_book.mutations)
        if existing_index != mutation_index
        if (recipe_id := minecraft_recipe_mutation_id(existing_mutation)) is not None
    }
    recipe_id = generated_minecraft_recipe_mutation_id(
        minecraft_username=minecraft_username,
        mutation=mutation,
        existing_recipe_ids=existing_recipe_ids,
    )
    return minecraft_recipe_mutation_with_id(mutation, recipe_id)
