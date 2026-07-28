"""Shared contracts for independently registered node API route domains."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, TypeAlias

from fastapi import Request

from node_auth import NodeAccessGrant, NodeApiScope


HttpExceptionFactory: TypeAlias = Callable[[int, str], Exception]


class MappingResponse(Protocol):
    """A domain response that can be encoded for the HTTP API."""

    def to_mapping(self) -> dict[str, object]: ...


class NodeAuthenticatedRouteService(Protocol):
    """Authentication operations shared by node API route registrars."""

    @property
    def node_name(self) -> str: ...

    def _require_access(
        self,
        request: Request,
        access_token: str | None,
        *,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
        token_node_names: Sequence[str] | None = None,
    ) -> NodeAccessGrant | None: ...

    def _request_actor_user_id(
        self,
        *,
        request: Request,
        access_token: str | None,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
        verified_grant: NodeAccessGrant | None = None,
    ) -> int: ...
