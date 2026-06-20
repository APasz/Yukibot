from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dev_cluster import (
    ClusterMember,
    ClusterPorts,
    build_process_environment,
    load_dotenv_values,
    settings_from_environment,
)


class DevClusterDotenvTests(unittest.TestCase):
    def test_load_dotenv_values_parses_spacing_and_quotes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
                '\n'.join(
                    [
                        "# comment",
                        " BOT_TOKEN = yuki-token ",
                        "ERIN_BOT_TOKEN='erin-token'",
                        'export DATA_AUTHORITY_TOKEN = "authority-secret"',
                    ]
                ),
                encoding="utf-8",
            )

            values = load_dotenv_values(env_file)

        self.assertEqual(
            values,
            {
                "BOT_TOKEN": "yuki-token",
                "ERIN_BOT_TOKEN": "erin-token",
                "DATA_AUTHORITY_TOKEN": "authority-secret",
            },
        )

    def test_load_dotenv_values_rejects_invalid_line(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text("BROKEN LINE", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "KEY=VALUE"):
                load_dotenv_values(env_file)


class DevClusterSettingsTests(unittest.TestCase):
    def test_settings_from_environment_uses_expected_token_fallbacks(self) -> None:
        settings = settings_from_environment(
            env={
                "BOT_TOKEN": "yuki-token",
                "ERIN_BOT_TOKEN": "erin-token",
                "DATA_AUTHORITY_TOKEN": "authority-token",
            },
            env_file=Path(".env"),
        )

        self.assertEqual(settings.yuki_token, "yuki-token")
        self.assertEqual(settings.erin_token, "erin-token")
        self.assertEqual(settings.authority_token, "authority-token")

    def test_settings_from_environment_requires_erin_token(self) -> None:
        with self.assertRaisesRegex(ValueError, "Erin bot token"):
            settings_from_environment(
                env={
                    "BOT_TOKEN": "yuki-token",
                    "DATA_AUTHORITY_TOKEN": "authority-token",
                },
                env_file=Path(".env"),
            )


class DevClusterEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base_env = {
            "DATA_AUTHORITY_TOKEN": "authority-token",
            "BOT_TOKEN": "yuki-token",
            "ERIN_BOT_TOKEN": "erin-token",
            "MOD_WEB_DISCORD_CLIENT_ID": "discord-client-id",
        }
        self.settings = settings_from_environment(env=self.base_env, env_file=Path(".env"))

    def test_build_process_environment_for_yuki_sets_local_authority_and_node_api(self) -> None:
        env = build_process_environment(
            base_env=self.base_env,
            settings=self.settings,
            member=ClusterMember.YUKI,
        )

        self.assertEqual(env["BOT_PROFILE"], "yuki")
        self.assertEqual(env["NODE_NAME"], "yuki")
        self.assertEqual(env["BOT_TOKEN"], "yuki-token")
        self.assertEqual(env["PUBLIC_BASE_URL"], "http://127.0.0.1:3180")
        self.assertEqual(env["MOD_WEB_PUBLIC_BASE_URL"], "http://127.0.0.1:3180")
        self.assertEqual(env["NODE_API_PUBLIC_BASE_URL"], "http://127.0.0.1:8082")
        self.assertEqual(env["NODE_API_PORT"], "8082")
        self.assertEqual(env["DATA_AUTHORITY_HOST"], "http://127.0.0.1:8081")
        self.assertEqual(env["DATA_AUTHORITY_BIND_PORT"], "8081")
        self.assertEqual(env["INDEV"], "true")

    def test_build_process_environment_for_erin_clears_local_authority_binding(self) -> None:
        env = build_process_environment(
            base_env=self.base_env,
            settings=self.settings,
            member=ClusterMember.ERIN,
        )

        self.assertEqual(env["BOT_PROFILE"], "erin")
        self.assertEqual(env["BOT_TOKEN"], "erin-token")
        self.assertEqual(env["NODE_API_PUBLIC_BASE_URL"], "http://127.0.0.1:8083")
        self.assertEqual(env["NODE_API_PORT"], "8083")
        self.assertEqual(env["DATA_AUTHORITY_BIND_HOST"], "")
        self.assertEqual(env["DATA_AUTHORITY_BIND_PORT"], "")
        self.assertEqual(env["STARTED_CHANNEL"], "")

    def test_build_process_environment_for_portal_clears_bot_and_node_api_settings(self) -> None:
        env = build_process_environment(
            base_env=self.base_env,
            settings=self.settings,
            member=ClusterMember.PORTAL,
        )

        self.assertEqual(env["BOT_PROFILE"], "portal")
        self.assertEqual(env["BOT_TOKEN"], "")
        self.assertEqual(env["NODE_API_PORT"], "")
        self.assertEqual(env["NODE_API_PUBLIC_BASE_URL"], "")
        self.assertEqual(env["PUBLIC_BASE_URL"], "http://127.0.0.1:3180")

    def test_cluster_ports_build_node_api_base_url_for_members(self) -> None:
        ports = ClusterPorts(
            bind_host="127.0.0.1",
            portal_port=3180,
            authority_port=8081,
            yuki_node_api_port=8082,
            erin_node_api_port=8083,
        )

        self.assertEqual(ports.node_api_base_url(ClusterMember.YUKI), "http://127.0.0.1:8082")
        self.assertEqual(ports.node_api_base_url(ClusterMember.ERIN), "http://127.0.0.1:8083")
        with self.assertRaisesRegex(ValueError, "does not expose"):
            ports.node_api_base_url(ClusterMember.PORTAL)


if __name__ == "__main__":
    unittest.main()
