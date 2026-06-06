from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum


class NodeApiScope(StrEnum):
    APPS_READ = "apps:read"
    MAP_READ = "map:read"
    MAP_WRITE = "map:write"
    CHAT_READ = "chat:read"
    CHAT_WRITE = "chat:write"
    MODS_READ = "mods:read"
    MODS_DOWNLOAD = "mods:download"
    MODS_WRITE = "mods:write"
    CONFIGS_READ = "configs:read"
    CONFIGS_WRITE = "configs:write"
    SAVES_READ = "saves:read"
    SAVES_DOWNLOAD = "saves:download"
    SAVES_WRITE = "saves:write"
    BLUEPRINTS_READ = "blueprints:read"
    BLUEPRINTS_WRITE = "blueprints:write"
    SETTINGS_READ = "settings:read"
    SETTINGS_WRITE = "settings:write"
    FILES_READ = "files:read"
    FILES_DOWNLOAD = "files:download"
    FILES_UPLOAD = "files:upload"
    APP_CONTROL = "app:control"
    APP_MANAGE = "app:manage"
    RELAY_TTS = "relay:tts"


class NodeTokenError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class NodeAccessGrant:
    subject: str
    node: str
    app: str | None
    scopes: frozenset[NodeApiScope]
    expires_at: int


def issue_node_token(*, secret: str, grant: NodeAccessGrant) -> str:
    if not secret:
        raise ValueError("Node token secret must not be empty.")
    if not grant.scopes:
        raise ValueError("Node token grant must include at least one scope.")

    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": grant.subject,
        "node": grant.node,
        "app": grant.app,
        "scopes": sorted(scope.value for scope in grant.scopes),
        "exp": grant.expires_at,
    }
    signing_input = ".".join((_encode_json(header), _encode_json(payload)))
    signature = _sign(secret=secret, signing_input=signing_input)
    return f"{signing_input}.{signature}"


def verify_node_token(
    *,
    secret: str,
    token: str,
    node: str,
    app: str | None,
    required_scopes: Collection[NodeApiScope],
    now: int | None = None,
) -> NodeAccessGrant:
    if not secret:
        raise NodeTokenError("Node token secret is not configured.")
    if not token:
        raise NodeTokenError("Node token is missing.")

    try:
        header_text, payload_text, signature = token.split(".", 2)
    except ValueError as xcp:
        raise NodeTokenError("Node token must have three sections.") from xcp

    signing_input = f"{header_text}.{payload_text}"
    expected_signature = _sign(secret=secret, signing_input=signing_input)
    if not hmac.compare_digest(signature, expected_signature):
        raise NodeTokenError("Node token signature is invalid.")

    header = _decode_json(header_text)
    if header.get("alg") != "HS256" or header.get("typ") != "JWT":
        raise NodeTokenError("Node token header is invalid.")

    payload = _decode_json(payload_text)
    grant = _grant_from_payload(payload)
    current_time = int(time.time()) if now is None else now
    if grant.expires_at <= current_time:
        raise NodeTokenError("Node token has expired.")
    if grant.node != node:
        raise NodeTokenError("Node token was issued for a different node.")
    if app is not None and grant.app not in {None, app}:
        raise NodeTokenError("Node token was issued for a different app.")

    missing_scopes = set(required_scopes) - grant.scopes
    if missing_scopes:
        missing_text = ", ".join(sorted(scope.value for scope in missing_scopes))
        raise NodeTokenError(f"Node token is missing required scopes: {missing_text}.")
    return grant


def _grant_from_payload(payload: dict[str, object]) -> NodeAccessGrant:
    subject = payload.get("sub")
    node = payload.get("node")
    app = payload.get("app")
    scopes = payload.get("scopes")
    expires_at = payload.get("exp")

    if not isinstance(subject, str) or not subject:
        raise NodeTokenError("Node token subject is invalid.")
    if not isinstance(node, str) or not node:
        raise NodeTokenError("Node token node is invalid.")
    if app is not None and not isinstance(app, str):
        raise NodeTokenError("Node token app is invalid.")
    if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
        raise NodeTokenError("Node token scopes are invalid.")
    if not isinstance(expires_at, int):
        raise NodeTokenError("Node token expiry is invalid.")

    try:
        resolved_scopes = frozenset(NodeApiScope(scope) for scope in scopes)
    except ValueError as xcp:
        raise NodeTokenError("Node token contains an unknown scope.") from xcp

    return NodeAccessGrant(
        subject=subject,
        node=node,
        app=app,
        scopes=resolved_scopes,
        expires_at=expires_at,
    )


def _encode_json(payload: Mapping[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64encode(raw)


def _decode_json(payload: str) -> dict[str, object]:
    try:
        raw = _b64decode(payload)
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as xcp:
        raise NodeTokenError("Node token JSON is invalid.") from xcp
    if not isinstance(data, dict):
        raise NodeTokenError("Node token JSON must be an object.")
    return data


def _sign(*, secret: str, signing_input: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(raw: str) -> bytes:
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))
