from __future__ import annotations

import logging
import unittest
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from fastapi import HTTPException, Request, WebSocket

import config
from _security import Power_Level
from mod_web_auth import ModWebUser
from node_api.request_auth import NodeRequestAuth, NodeRequestContext
from node_auth import NodeAccessGrant, NodeApiScope, issue_node_token


def _request(*, authorization: str | None = None) -> Request:
    headers = [] if authorization is None else [(b"authorization", authorization.encode())]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/node/apps",
            "headers": headers,
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        }
    )


def _auth() -> NodeRequestAuth:
    return NodeRequestAuth(
        node_name=lambda: "erin",
        http_exception=lambda status_code, detail: HTTPException(
            status_code=status_code,
            detail=detail,
        ),
        logger=logging.getLogger(__name__),
    )


class NodeRequestAuthTests(unittest.TestCase):
    def test_token_access_returns_verified_grant_and_resolves_web_actor(self) -> None:
        server = replace(config.MOD_WEB_SERVER, node_name="erin", token_secret="secret")
        token = issue_node_token(
            secret="secret",
            grant=NodeAccessGrant(
                subject="web:42",
                node="erin",
                app="minecraft_alpha",
                scopes=frozenset({NodeApiScope.MODS_WRITE}),
                expires_at=2_000_000_000,
            ),
        )
        with patch.object(config, "MOD_WEB_SERVER", server):
            context = _auth().require_access(
                _request(authorization=f"Bearer {token}"),
                None,
                app_name="minecraft_alpha",
                scopes=(NodeApiScope.MODS_WRITE,),
            )

        self.assertIsNotNone(context.grant)
        self.assertIsNone(context.actor_user_id)
        self.assertEqual(_auth().require_actor(context).actor_user_id, 42)

    def test_token_access_does_not_read_a_web_session(self) -> None:
        server = replace(config.MOD_WEB_SERVER, node_name="erin", token_secret="secret")
        token = issue_node_token(
            secret="secret",
            grant=NodeAccessGrant(
                subject="web:42",
                node="erin",
                app="minecraft_alpha",
                scopes=frozenset({NodeApiScope.MODS_WRITE}),
                expires_at=2_000_000_000,
            ),
        )
        web_auth = Mock()
        web_auth.current_user.side_effect = AssertionError("token access must not read a web session")
        auth = _auth()
        auth.set_web_auth(cast(Any, web_auth))

        with patch.object(config, "MOD_WEB_SERVER", server):
            context = auth.require_access(
                _request(authorization=f"Bearer {token}"),
                None,
                app_name="minecraft_alpha",
                scopes=(NodeApiScope.MODS_WRITE,),
            )

        self.assertIsNotNone(context.grant)
        self.assertIsNone(context.web_user)
        web_auth.current_user.assert_not_called()

    def test_with_current_web_user_enriches_context_immutably(self) -> None:
        user = ModWebUser(discord_id=42, username="Tester", global_name=None, avatar_hash=None)
        web_auth = Mock()
        web_auth.current_user.return_value = user
        auth = _auth()
        auth.set_web_auth(cast(Any, web_auth))
        original = NodeRequestContext(grant=None, actor_user_id=7)

        enriched = auth.with_current_web_user(original, _request())

        self.assertIsNone(original.web_user)
        self.assertEqual(enriched.actor_user_id, 7)
        self.assertIs(enriched.web_user, user)
        web_auth.current_user.assert_called_once()

    def test_web_session_access_returns_user_and_actor_in_immutable_context(self) -> None:
        server = replace(config.MOD_WEB_SERVER, token_secret=None)
        user = ModWebUser(discord_id=42, username="Tester", global_name=None, avatar_hash=None)
        web_auth = Mock()
        web_auth.enabled = True
        web_auth.current_user.return_value = user
        acl = Mock()
        acl.can.return_value = True
        acl.level_of.return_value = Power_Level.user
        auth = _auth()
        auth.set_web_auth(cast(Any, web_auth))
        auth.set_acl(cast(Any, acl))

        with patch.object(config, "MOD_WEB_SERVER", server):
            context = auth.require_access(
                _request(),
                None,
                app_name="minecraft_alpha",
                scopes=(NodeApiScope.MODS_WRITE,),
            )

        actor_context = auth.require_actor(context)
        self.assertIsNone(context.grant)
        self.assertIs(context.web_user, user)
        self.assertIsNone(context.actor_user_id)
        self.assertEqual(actor_context.actor_user_id, 42)
        self.assertIs(actor_context.web_user, user)
        self.assertIsNone(context.actor_user_id)
        self.assertIs(auth.with_current_web_user(context, _request()), context)
        web_auth.current_user.assert_called_once()

    def test_websocket_access_uses_the_same_token_verification_service(self) -> None:
        server = replace(config.MOD_WEB_SERVER, node_name="erin", token_secret="secret")
        token = issue_node_token(
            secret="secret",
            grant=NodeAccessGrant(
                subject="stream-client",
                node="erin",
                app=None,
                scopes=frozenset({NodeApiScope.APPS_READ}),
                expires_at=2_000_000_000,
            ),
        )
        websocket = cast(WebSocket, SimpleNamespace(headers={"authorization": f"Bearer {token}"}))
        with patch.object(config, "MOD_WEB_SERVER", server):
            context = _auth().require_websocket_token_access(
                websocket=websocket,
                access_token="ignored-query-token",
                app_name=None,
                scopes=(NodeApiScope.APPS_READ,),
            )

        self.assertIsNotNone(context.grant)
        self.assertEqual(context.grant.subject, "stream-client")


if __name__ == "__main__":
    unittest.main()
