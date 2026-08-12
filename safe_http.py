"""Safe outbound HTTP primitives for user-supplied links."""

from __future__ import annotations

import ipaddress
import socket
from typing import Protocol
from urllib.parse import SplitResult, urlsplit

from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver


class UnsafeOutboundUrlError(ValueError):
    """Raised when an outbound URL could target a non-public network address."""


class _HostResolver(Protocol):
    """The resolver operations needed to validate outbound destinations."""

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AddressFamily.AF_INET,
    ) -> list[ResolveResult]: ...

    async def close(self) -> None: ...


def validate_public_http_url(url: str) -> SplitResult:
    """Validate an HTTP(S) URL before an outbound request is made."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeOutboundUrlError("URL must use HTTP(S) and include a host.")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeOutboundUrlError("URL userinfo is not allowed.")
    try:
        port = parsed.port
    except ValueError as xcp:
        raise UnsafeOutboundUrlError("URL port is invalid.") from xcp
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeOutboundUrlError("URL port is invalid.")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return parsed
    if not address.is_global:
        raise UnsafeOutboundUrlError("URL must resolve to a public address.")
    return parsed


class PublicAddressResolver(AbstractResolver):
    """Resolve hosts only when every returned address is globally routable."""

    def __init__(self) -> None:
        self._resolver: _HostResolver = DefaultResolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AddressFamily.AF_INET,
    ) -> list[ResolveResult]:
        records = await self._resolver.resolve(host, port, family=family)
        if not records:
            raise UnsafeOutboundUrlError("URL host did not resolve.")
        for record in records:
            try:
                address = ipaddress.ip_address(record["host"])
            except ValueError as xcp:
                raise UnsafeOutboundUrlError("URL host resolved to an invalid address.") from xcp
            if not address.is_global:
                raise UnsafeOutboundUrlError("URL host resolved to a non-public address.")
        return records

    async def close(self) -> None:
        await self._resolver.close()
