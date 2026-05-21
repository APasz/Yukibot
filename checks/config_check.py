from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import config


class ConfigPublicUrlTests(unittest.TestCase):
    def test_resolve_public_base_url_adds_https_for_bare_host(self) -> None:
        self.assertEqual(
            config.resolve_public_base_url("wakusei.apasz.com"),
            "https://wakusei.apasz.com",
        )

    def test_resolve_public_base_url_rejects_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "PUBLIC_BASE_URL must not include a path."):
            config.resolve_public_base_url("https://wakusei.apasz.com/files")

    def test_resolve_public_uploads_base_url_uses_uploads_path(self) -> None:
        self.assertEqual(
            config.resolve_public_uploads_base_url("https://wakusei.apasz.com"),
            "https://wakusei.apasz.com/uploads/",
        )

    def test_resolve_public_addr_uses_host_from_base_url(self) -> None:
        self.assertEqual(
            config.resolve_public_addr("https://wakusei.apasz.com", public_ip="203.0.113.10"),
            "wakusei.apasz.com",
        )

    def test_resolve_public_addr_defaults_to_public_ip(self) -> None:
        self.assertEqual(
            config.resolve_public_addr(None, public_ip="203.0.113.10"),
            "203.0.113.10",
        )


class ConfigDataAuthorityTests(unittest.TestCase):
    def test_erin_profile_includes_activity_service(self) -> None:
        self.assertTrue(config.BOT_PROFILES[config.BotProfileName.ERIN].has_service(config.BotService.ACTIVITY))

    def test_resolve_data_authority_endpoint_defaults_to_https_for_bare_host_without_public_hint(self) -> None:
        endpoint = config.resolve_data_authority_endpoint(
            "wakusei.apasz.com",
            None,
            mode=config.DataAuthorityMode.REMOTE,
            public_base_url="https://ignored.example",
        )

        self.assertEqual(
            endpoint,
            config.AuthorityEndpoint(scheme="https", host="wakusei.apasz.com", port=443),
        )

    def test_resolve_data_authority_endpoint_inherits_http_from_explicit_public_url_base(self) -> None:
        endpoint = config.resolve_data_authority_endpoint(
            "wakusei.apasz.com",
            None,
            mode=config.DataAuthorityMode.REMOTE,
            public_base_url="http://wakusei.apasz.com",
            raw_public_base_url="http://wakusei.apasz.com",
        )

        self.assertEqual(
            endpoint,
            config.AuthorityEndpoint(scheme="http", host="wakusei.apasz.com", port=80),
        )

    def test_resolve_data_authority_endpoint_uses_public_url_base_for_yuki(self) -> None:
        endpoint = config.resolve_data_authority_endpoint(
            None,
            None,
            mode=config.DataAuthorityMode.LOCAL,
            public_base_url="http://authority.apasz.com",
            raw_public_base_url="http://authority.apasz.com",
        )

        self.assertEqual(
            endpoint,
            config.AuthorityEndpoint(scheme="http", host="authority.apasz.com", port=80),
        )

    def test_resolve_data_authority_endpoint_uses_explicit_public_url_base_for_remote_when_host_missing(self) -> None:
        endpoint = config.resolve_data_authority_endpoint(
            None,
            None,
            mode=config.DataAuthorityMode.REMOTE,
            public_base_url="http://wakusei.apasz.com",
            raw_public_base_url="http://wakusei.apasz.com",
        )

        self.assertEqual(
            endpoint,
            config.AuthorityEndpoint(scheme="http", host="wakusei.apasz.com", port=80),
        )

    def test_resolve_data_authority_endpoint_requires_remote_host_or_explicit_public_url_base(self) -> None:
        self.assertIsNone(
            config.resolve_data_authority_endpoint(
                None,
                None,
                mode=config.DataAuthorityMode.REMOTE,
                public_base_url="https://ignored.example",
            )
        )

    def test_resolve_data_authority_endpoint_rejects_path_in_data_authority_host(self) -> None:
        with self.assertRaisesRegex(ValueError, "DATA_AUTHORITY_HOST must not include a path."):
            config.resolve_data_authority_endpoint(
                "https://wakusei.apasz.com/authority",
                None,
                mode=config.DataAuthorityMode.REMOTE,
                public_base_url="https://ignored.example",
            )

    def test_resolve_data_authority_server_binding_defaults_to_endpoint(self) -> None:
        endpoint = config.AuthorityEndpoint(scheme="http", host="wakusei.apasz.com", port=80)

        binding = config.resolve_data_authority_server_binding(None, None, endpoint=endpoint)

        self.assertEqual(binding, config.AuthorityServerBinding(host="wakusei.apasz.com", port=80))

    def test_resolve_data_authority_server_binding_allows_override(self) -> None:
        endpoint = config.AuthorityEndpoint(scheme="http", host="wakusei.apasz.com", port=80)

        binding = config.resolve_data_authority_server_binding("127.0.0.1", 8081, endpoint=endpoint)

        self.assertEqual(binding, config.AuthorityServerBinding(host="127.0.0.1", port=8081))

    def test_resolve_data_authority_server_binding_rejects_urls(self) -> None:
        endpoint = config.AuthorityEndpoint(scheme="http", host="wakusei.apasz.com", port=80)

        with self.assertRaisesRegex(ValueError, "DATA_AUTHORITY_BIND_HOST must be a plain host or interface"):
            config.resolve_data_authority_server_binding("http://127.0.0.1", None, endpoint=endpoint)

    def test_load_authority_json_uses_local_snapshot_when_remote_and_cache_are_unavailable(self) -> None:
        with TemporaryDirectory() as tmp:
            local_path = Path(tmp) / "users.json"
            local_path.write_text('{"admin": [42]}', encoding="utf-8")

            with (
                patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.REMOTE),
                patch.object(config, "DATA_AUTHORITY_CACHE_DIR", Path(tmp) / "cache"),
                patch.object(config, "fetch_remote_resource", side_effect=TimeoutError("offline")),
            ):
                payload = config.load_authority_json(config.AuthorityResource.USERS, local_path)

        self.assertEqual(payload, {"admin": [42]})


if __name__ == "__main__":
    unittest.main()
