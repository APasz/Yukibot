from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from urllib.parse import parse_qs, urlsplit

from fastapi import Request  # pyright: ignore[reportMissingImports]

import config
from _security import Access_Control, Power_Level
from mod_web_auth import (
    _SESSION_COOKIE_NAME,
    ModWebAuthService,
    ModWebUser,
)
from node_api import NodeApiScope, NodeApiService


class FakeRequest:
    def __init__(self, *, cookies: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers = headers or {}


class ModWebAuthTests(unittest.TestCase):
    @staticmethod
    def _set_cookie_headers(response: object) -> list[str]:
        raw_headers = getattr(response, "raw_headers")
        return [value.decode("latin-1") for key, value in raw_headers if key == b"set-cookie"]

    def _cookie_value(self, response: object, cookie_name: str) -> str:
        prefix = f"{cookie_name}="
        for header in self._set_cookie_headers(response):
            if not header.startswith(prefix):
                continue
            return header[len(prefix) :].split(";", 1)[0]
        self.fail(f"Missing cookie {cookie_name!r}")

    def test_login_redirect_builds_discord_oauth_url_and_state_cookie(self) -> None:
        auth = ModWebAuthService(
            config.ModWebAuthConfig(
                discord_client_id="123456789012345678",
                discord_client_secret="secret",
                redirect_url="https://mods.example/auth/discord/callback",
            )
        )

        response = auth.login_redirect(next_path="/mods/minecraft")
        location = response.headers["location"]
        parsed = urlsplit(location)
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "discord.com")
        self.assertEqual(parsed.path, "/oauth2/authorize")
        self.assertEqual(query["client_id"], ["123456789012345678"])
        self.assertEqual(query["redirect_uri"], ["https://mods.example/auth/discord/callback"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["scope"], ["identify"])
        self.assertTrue(any("yukibot_mod_web_oauth_state" in header for header in self._set_cookie_headers(response)))

    def test_current_user_resolves_server_side_session_cookie(self) -> None:
        auth = ModWebAuthService(
            config.ModWebAuthConfig(
                discord_client_id="123456789012345678",
                discord_client_secret="secret",
                redirect_url="https://mods.example/auth/discord/callback",
            )
        )
        user = ModWebUser(discord_id=42, username="tester", global_name="Tester", avatar_hash=None)
        session = auth._create_session(user)
        request = cast(Request, FakeRequest(cookies={_SESSION_COOKIE_NAME: session.session_id}))

        self.assertEqual(auth.current_user(request), user)

    def test_dev_login_response_creates_session_for_requested_level(self) -> None:
        auth = ModWebAuthService(
            config.ModWebAuthConfig(
                discord_client_id=None,
                discord_client_secret=None,
                redirect_url="http://localhost:3180/auth/discord/callback",
                bypass_enabled=True,
            )
        )
        response = auth.dev_login_response(level=Power_Level.admin, next_path="/mod-web")
        session_id = self._cookie_value(response, _SESSION_COOKIE_NAME)
        request = cast(Request, FakeRequest(cookies={_SESSION_COOKIE_NAME: session_id}))
        user = auth.current_user(request)

        self.assertEqual(response.headers["location"], "/mod-web")
        self.assertIsNotNone(user)
        assert user is not None
        self.assertEqual(user.discord_id, Access_Control.dev_bypass_user_id(Power_Level.admin))
        self.assertEqual(user.username, "Dev Admin")
        self.assertEqual(user.global_name, "Agent Smith")
        self.assertEqual(user.display_name, "Agent Smith")

    def test_logout_response_clears_session_cookie(self) -> None:
        auth = ModWebAuthService(
            config.ModWebAuthConfig(
                discord_client_id=None,
                discord_client_secret=None,
                redirect_url="http://localhost:3180/auth/discord/callback",
                bypass_enabled=True,
            )
        )

        response = auth.logout_response()

        self.assertTrue(
            any(header.startswith(f"{_SESSION_COOKIE_NAME}=") for header in self._set_cookie_headers(response))
        )

    def test_bypass_login_redirect_creates_default_dev_session(self) -> None:
        auth = ModWebAuthService(
            config.ModWebAuthConfig(
                discord_client_id=None,
                discord_client_secret=None,
                redirect_url="http://localhost:3180/auth/discord/callback",
                bypass_enabled=True,
            )
        )

        response = auth.login_redirect(next_path="/mod-web")
        session_id = self._cookie_value(response, _SESSION_COOKIE_NAME)
        request = cast(Request, FakeRequest(cookies={_SESSION_COOKIE_NAME: session_id}))
        user = auth.current_user(request)

        self.assertEqual(response.headers["location"], "/mod-web")
        self.assertIsNotNone(user)
        assert user is not None
        self.assertEqual(user.discord_id, Access_Control.dev_bypass_user_id(Power_Level.user))

    def test_node_api_accepts_authorised_web_session_without_node_token(self) -> None:
        auth = ModWebAuthService(
            config.ModWebAuthConfig(
                discord_client_id="123456789012345678",
                discord_client_secret="secret",
                redirect_url="https://mods.example/auth/discord/callback",
            )
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        session = auth._create_session(user)

        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "users.json"
            pointer.write_text(json.dumps({"user": [42]}))
            acl = Access_Control(pointer)

        service = NodeApiService()
        service.set_web_auth(auth)
        service.set_acl(acl)
        request = cast(
            Request,
            FakeRequest(cookies={_SESSION_COOKIE_NAME: session.session_id}, headers={}),
        )

        service._require_access(
            request,
            access_token=None,
            app_name="minecraft",
            scopes=(NodeApiScope.MODS_DOWNLOAD,),
        )

    def test_node_api_visitor_session_can_use_home_and_chat_scopes_only(self) -> None:
        auth = ModWebAuthService(
            config.ModWebAuthConfig(
                discord_client_id="123456789012345678",
                discord_client_secret="secret",
                redirect_url="https://mods.example/auth/discord/callback",
            )
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        session = auth._create_session(user)

        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "users.json"
            pointer.write_text(json.dumps({"visitor": [42]}))
            acl = Access_Control(pointer)

        service = NodeApiService()
        service.set_web_auth(auth)
        service.set_acl(acl)
        request = cast(Request, FakeRequest(cookies={_SESSION_COOKIE_NAME: session.session_id}, headers={}))

        service._require_access(
            request,
            access_token=None,
            app_name=None,
            scopes=(NodeApiScope.APPS_READ,),
        )
        service._require_access(
            request,
            access_token=None,
            app_name="minecraft",
            scopes=(NodeApiScope.CHAT_WRITE,),
        )
        with self.assertRaises(Exception) as raised:
            service._require_access(
                request,
                access_token=None,
                app_name="minecraft",
                scopes=(NodeApiScope.MODS_DOWNLOAD,),
            )

        self.assertEqual(getattr(raised.exception, "status_code"), 403)

    def test_node_api_dev_user_session_respects_selected_level(self) -> None:
        auth = ModWebAuthService(
            config.ModWebAuthConfig(
                discord_client_id=None,
                discord_client_secret=None,
                redirect_url="http://localhost:3180/auth/discord/callback",
                bypass_enabled=True,
            )
        )

        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "users.json"
            pointer.write_text(json.dumps({}))
            acl = Access_Control(pointer)

        service = NodeApiService()
        service.set_web_auth(auth)
        service.set_acl(acl)
        response = auth.dev_login_response(level=Power_Level.user, next_path="/")
        session_id = self._cookie_value(response, _SESSION_COOKIE_NAME)
        request = cast(Request, FakeRequest(cookies={_SESSION_COOKIE_NAME: session_id}, headers={}))

        service._require_access(
            request,
            access_token=None,
            app_name="minecraft",
            scopes=(NodeApiScope.MODS_DOWNLOAD,),
        )
        with self.assertRaises(Exception) as raised:
            service._require_access(
                request,
                access_token=None,
                app_name="minecraft",
                scopes=(NodeApiScope.CONFIGS_WRITE,),
            )

        self.assertEqual(getattr(raised.exception, "status_code"), 403)

    def test_node_api_dev_sudo_session_allows_sudo_scope(self) -> None:
        auth = ModWebAuthService(
            config.ModWebAuthConfig(
                discord_client_id=None,
                discord_client_secret=None,
                redirect_url="http://localhost:3180/auth/discord/callback",
                bypass_enabled=True,
            )
        )

        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "users.json"
            pointer.write_text(json.dumps({}))
            acl = Access_Control(pointer)

        service = NodeApiService()
        service.set_web_auth(auth)
        service.set_acl(acl)
        response = auth.dev_login_response(level=Power_Level.sudo, next_path="/")
        session_id = self._cookie_value(response, _SESSION_COOKIE_NAME)
        request = cast(Request, FakeRequest(cookies={_SESSION_COOKIE_NAME: session_id}, headers={}))

        service._require_access(
            request,
            access_token=None,
            app_name="minecraft",
            scopes=(NodeApiScope.CONFIGS_WRITE,),
        )

    def test_bypass_flag_requires_indev(self) -> None:
        base = config.ModWebAuthConfig(
            discord_client_id=None,
            discord_client_secret=None,
            redirect_url="http://localhost:3180/auth/discord/callback",
            bypass_enabled=False,
        )

        self.assertFalse(base.enabled)
        self.assertTrue(replace(base, bypass_enabled=True).enabled)


if __name__ == "__main__":
    unittest.main()
