from __future__ import annotations

import logging
import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch

import hikari
from hikari import applications
from modmux.models import Provider

import config
from apps._config import (
    AppVersion,
    App_Config,
    ClientPackKubeJsScript,
    ClientPackMetadataConfig,
    ClientPackRelease,
    ModDistributionMode,
    mod_capabilities_for_scope,
    next_client_pack_version,
)
from restart_targets import RestartTarget


class ConfigEnvFlagTests(unittest.TestCase):
    def test_parse_env_flag_requires_explicit_true_value(self) -> None:
        self.assertTrue(config._parse_env_flag("True", var_name="INDEV"))
        self.assertFalse(config._parse_env_flag("False", var_name="INDEV"))
        self.assertFalse(config._parse_env_flag(None, var_name="INDEV"))

    def test_parse_env_flag_rejects_ambiguous_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "INDEV must be a boolean flag"):
            config._parse_env_flag("development", var_name="INDEV")


class AppModCapabilitiesTests(unittest.TestCase):
    def test_minecraft_supports_launcher_client_packs(self) -> None:
        capabilities = mod_capabilities_for_scope("minecraft")

        self.assertEqual(capabilities.mode, ModDistributionMode.MINECRAFT_LAUNCHER_PACK)
        self.assertTrue(capabilities.supports_client_pack)
        self.assertTrue(capabilities.supports_launcher_formats)
        self.assertTrue(capabilities.include_client_overrides)
        self.assertEqual(
            capabilities.launcher_metadata_providers,
            (Provider.MODRINTH, Provider.CURSEFORGE),
        )

    def test_client_pack_versions_are_date_based_and_sequence_same_day_releases(self) -> None:
        published_on = date(2026, 7, 4)

        self.assertEqual(next_client_pack_version(None, published_on=published_on), "2026-07-04")
        self.assertEqual(
            next_client_pack_version("2026-07-04", published_on=published_on),
            "2026-07-04.2",
        )
        self.assertEqual(
            next_client_pack_version("2026-07-04.2", published_on=published_on),
            "2026-07-04.3",
        )
        self.assertEqual(next_client_pack_version("7", published_on=published_on), "2026-07-04")

    def test_client_pack_release_normalises_and_requires_versioned_changes(self) -> None:
        release = ClientPackRelease(version=" 2026-07-04 ", changelog=" Added renderer options. ")

        self.assertEqual(release.version, "2026-07-04")
        self.assertEqual(release.changelog, "Added renderer options.")
        with self.assertRaisesRegex(ValueError, "requires a version"):
            ClientPackRelease(version="", changelog="Changes")
        with self.assertRaisesRegex(ValueError, "requires a changelog"):
            ClientPackRelease(version="2026-07-04", changelog="  ")

    def test_client_pack_kubejs_paths_are_typed_and_restricted_to_script_roots(self) -> None:
        script = ClientPackKubeJsScript(
            relative_path=" server_scripts/recipes/custom.js ",
            included=False,
        )

        self.assertEqual(script.relative_path, "server_scripts/recipes/custom.js")
        self.assertFalse(script.included)
        with self.assertRaisesRegex(ValueError, "server_scripts or startup_scripts"):
            ClientPackKubeJsScript(relative_path="../secrets.txt")
        with self.assertRaisesRegex(ValueError, "example.js"):
            ClientPackKubeJsScript(relative_path="startup_scripts/example.js")

    def test_app_config_normalises_excluded_kubejs_script_paths(self) -> None:
        app_config = App_Config(
            name="minecraft_test",
            instance_key="test",
            friendly_name="Test",
            directory=Path("/tmp/minecraft-test"),
            apps_dir=Path("/tmp"),
            mods_dir=None,
            client_pack_excluded_kubejs_scripts=(
                "startup_scripts/registry.js",
                "server_scripts/events.js",
            ),
            scope="minecraft",
        )

        self.assertEqual(
            app_config.client_pack_excluded_kubejs_scripts,
            ("server_scripts/events.js", "startup_scripts/registry.js"),
        )

    def test_client_pack_metadata_validates_and_renders_filename_template(self) -> None:
        metadata = ClientPackMetadataConfig(
            name="Example Pack",
            description="Client performance and interface mods.",
            filename_template="{app_name}-{pack_name}-{version}-{minecraft_version}-{format}",
            include_servers_dat=False,
            include_options_txt=False,
        )

        self.assertFalse(metadata.include_servers_dat)
        self.assertFalse(metadata.include_options_txt)
        self.assertEqual(
            metadata.filename_stem(
                app_name="minecraft_alpha",
                version="2026-07-04.2",
                minecraft_version="1.21.1",
                format_name="modrinth",
            ),
            "minecraft_alpha-Example_Pack-2026-07-04.2-1.21.1-modrinth",
        )
        with self.assertRaisesRegex(ValueError, "unknown client-pack filename placeholder"):
            ClientPackMetadataConfig(name="Example", filename_template="{unknown}")
        with self.assertRaisesRegex(ValueError, "path separators"):
            ClientPackMetadataConfig(name="Example", filename_template="packs/{version}")

    def test_sevendays_supports_generic_client_packs_only(self) -> None:
        capabilities = mod_capabilities_for_scope("sevendays")

        self.assertTrue(capabilities.supports_client_pack)
        self.assertFalse(capabilities.supports_launcher_formats)

    def test_non_client_pack_scopes_have_explicit_modes(self) -> None:
        expected_modes = {
            "factorio": ModDistributionMode.RAW_ENABLED,
            "beammp": ModDistributionMode.SERVER_PUSH,
            "ets": ModDistributionMode.NONE,
            "satisfactory": ModDistributionMode.NONE,
        }

        for scope, expected_mode in expected_modes.items():
            with self.subTest(scope=scope):
                self.assertEqual(mod_capabilities_for_scope(scope).mode, expected_mode)
                self.assertFalse(mod_capabilities_for_scope(scope).supports_client_pack)


class ConfigLoggingTests(unittest.TestCase):
    def test_noisy_loggers_use_dedicated_non_propagating_files(self) -> None:
        expected_files = {
            config.LOGGER_TRAFFIC: "Traffic.log",
            config.LOGGER_TTS: "TTS.log",
            config.LOGGER_AUDIT: "Audit.log",
        }

        for logger_name, filename in expected_files.items():
            with self.subTest(logger_name=logger_name):
                logger = logging.getLogger(logger_name)
                file_names = {
                    Path(handler.baseFilename).name
                    for handler in logger.handlers
                    if isinstance(handler, logging.FileHandler)
                }
                self.assertIn(filename, file_names)
                self.assertFalse(logger.propagate)

    def test_known_warning_filter_suppresses_websocket_deprecation_noise(self) -> None:
        warning_filter = config.SuppressKnownWarningsFilter()
        warning_record = logging.LogRecord(
            name="py.warnings",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="websockets.server.WebSocketServerProtocol is deprecated",
            args=(),
            exc_info=None,
        )
        regular_record = logging.LogRecord(
            name="py.warnings",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="some other warning",
            args=(),
            exc_info=None,
        )

        self.assertFalse(warning_filter.filter(warning_record))
        self.assertTrue(warning_filter.filter(regular_record))


class AppVersionTests(unittest.TestCase):
    def test_display_value_includes_steam_manifest_metadata(self) -> None:
        self.assertEqual(
            AppVersion(
                main="1.2.3",
                build=42,
                steam_branch="experimental",
                steam_build=987654,
            ).display_value,
            "1.2.3:42 [Steam experimental build 987654]",
        )


class ConfigPublicUrlTests(unittest.TestCase):
    def test_resolve_public_base_url_adds_https_for_bare_host(self) -> None:
        with patch.object(config, "INDEV", False):
            self.assertEqual(
                config.resolve_public_base_url("wakusei.apasz.com"),
                "https://wakusei.apasz.com",
            )

    def test_resolve_public_base_url_rejects_http_outside_indev(self) -> None:
        with patch.object(config, "INDEV", False):
            with self.assertRaisesRegex(ValueError, "PUBLIC_BASE_URL must use https outside INDEV."):
                config.resolve_public_base_url("http://wakusei.apasz.com")

    def test_resolve_public_base_url_allows_http_in_indev(self) -> None:
        with patch.object(config, "INDEV", True):
            self.assertEqual(
                config.resolve_public_base_url("http://127.0.0.1:3180"),
                "http://127.0.0.1:3180",
            )

    def test_resolve_public_base_url_rejects_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "PUBLIC_BASE_URL must not include a path."):
            config.resolve_public_base_url("https://wakusei.apasz.com/files")

    def test_resolve_public_uploads_base_url_uses_uploads_path(self) -> None:
        self.assertEqual(
            config.resolve_public_uploads_base_url("https://wakusei.apasz.com"),
            "https://wakusei.apasz.com/uploads/",
        )

    def test_resolve_mod_web_public_base_url_defaults_to_public_base_url(self) -> None:
        self.assertEqual(
            config.resolve_mod_web_public_base_url(
                None,
                public_base_url="https://wakusei.apasz.com",
            ),
            "https://wakusei.apasz.com",
        )

    def test_resolve_mod_web_public_base_url_preserves_explicit_url(self) -> None:
        with patch.object(config, "INDEV", True):
            self.assertEqual(
                config.resolve_mod_web_public_base_url(
                    "http://mods.apasz.com:8088",
                    public_base_url="https://wakusei.apasz.com",
                ),
                "http://mods.apasz.com:8088",
            )

    def test_resolve_mod_web_public_base_url_rejects_http_outside_indev(self) -> None:
        with patch.object(config, "INDEV", False):
            with self.assertRaisesRegex(ValueError, "MOD_WEB_PUBLIC_BASE_URL must use https outside INDEV."):
                config.resolve_mod_web_public_base_url(
                    "http://mods.apasz.com:8088",
                    public_base_url="https://wakusei.apasz.com",
                )

    def test_resolve_mod_web_public_base_url_defaults_bare_override_to_https(self) -> None:
        with patch.object(config, "INDEV", False):
            self.assertEqual(
                config.resolve_mod_web_public_base_url(
                    "10.0.0.173:3180",
                    public_base_url="https://wakusei.apasz.com",
                ),
                "https://10.0.0.173:3180",
            )

    def test_resolve_mod_web_public_base_url_defaults_bare_override_to_http_in_indev(self) -> None:
        with patch.object(config, "INDEV", True):
            self.assertEqual(
                config.resolve_mod_web_public_base_url(
                    "10.0.0.173:3180",
                    public_base_url="https://wakusei.apasz.com",
                ),
                "http://10.0.0.173:3180",
            )

    def test_parse_mod_web_build_sha_normalises_valid_commit(self) -> None:
        self.assertEqual(config.parse_mod_web_build_sha(" ABCDEF123456 "), "abcdef123456")
        self.assertIsNone(config.parse_mod_web_build_sha(None))

    def test_parse_mod_web_build_sha_rejects_invalid_commit(self) -> None:
        with self.assertRaisesRegex(ValueError, "7-40 character hexadecimal"):
            config.parse_mod_web_build_sha("not-a-commit")

    def test_resolve_node_api_base_url_uses_mod_web_host(self) -> None:
        self.assertEqual(
            config.resolve_node_api_base_url("http://mods.apasz.com:8088"),
            "http://mods.apasz.com:8088/api/node",
        )

    def test_resolve_node_api_public_base_url_defaults_to_mod_web_public_base_url(self) -> None:
        self.assertEqual(
            config.resolve_node_api_public_base_url(
                None,
                mod_web_public_base_url="https://mods.apasz.com",
            ),
            "https://mods.apasz.com",
        )

    def test_resolve_node_api_public_base_url_preserves_explicit_override(self) -> None:
        with patch.object(config, "INDEV", True):
            self.assertEqual(
                config.resolve_node_api_public_base_url(
                    "http://node-api.apasz.com:8089",
                    mod_web_public_base_url="https://mods.apasz.com",
                ),
                "http://node-api.apasz.com:8089",
            )

    def test_resolve_node_api_public_base_url_rejects_http_outside_indev(self) -> None:
        with patch.object(config, "INDEV", False):
            with self.assertRaisesRegex(ValueError, "NODE_API_PUBLIC_BASE_URL must use https outside INDEV."):
                config.resolve_node_api_public_base_url(
                    "http://node-api.apasz.com:8089",
                    mod_web_public_base_url="https://mods.apasz.com",
                )

    def test_normalise_google_font_source_url_converts_specimen_page(self) -> None:
        self.assertEqual(
            config.normalise_google_font_source_url(
                "https://fonts.google.com/specimen/Black+Ops+One?preview.script=Latn"
            ),
            "https://fonts.googleapis.com/css2?family=Black+Ops+One&display=swap",
        )

    def test_normalise_google_font_source_urls_accepts_multiline_text(self) -> None:
        self.assertEqual(
            config.normalise_google_font_source_urls(
                "https://fonts.google.com/specimen/Black+Ops+One\nhttps://fonts.googleapis.com/css2?family=Roboto&display=swap"
            ),
            (
                "https://fonts.googleapis.com/css2?family=Black+Ops+One&display=swap",
                "https://fonts.googleapis.com/css2?family=Roboto&display=swap",
            ),
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

    def test_build_discord_oauth_url_for_guild_install(self) -> None:
        self.assertEqual(
            config.build_discord_oauth_url(
                hikari.Snowflake(123456789012345678),
                install_type=config.OAuthInstallType.GUILD,
            ),
            "https://discord.com/oauth2/authorize?client_id=123456789012345678&integration_type=0&scope=applications.commands+bot",
        )

    def test_build_discord_oauth_url_for_user_install(self) -> None:
        self.assertEqual(
            config.build_discord_oauth_url(
                "123456789012345678",
                install_type=config.OAuthInstallType.USER,
            ),
            "https://discord.com/oauth2/authorize?client_id=123456789012345678&integration_type=1&scope=applications.commands",
        )


class ConfigProfileTests(unittest.TestCase):
    def test_parse_bot_profile_requires_env_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "BOT_PROFILE must be set"):
            config._parse_bot_profile(None)

    def test_parse_bot_profile_rejects_unknown_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "BOT_PROFILE must be one of: erin, portal, yuki"):
            config._parse_bot_profile("nope")


class ConfigDataAuthorityTests(unittest.TestCase):
    def test_erin_profile_includes_activity_service(self) -> None:
        self.assertTrue(config.BOT_PROFILES[config.BotProfileName.ERIN].has_service(config.BotService.ACTIVITY))

    def test_portal_profile_has_no_commands_or_services(self) -> None:
        profile = config.BOT_PROFILES[config.BotProfileName.PORTAL]

        self.assertEqual(profile.command_groups, ())
        self.assertEqual(profile.services, frozenset())

    def test_portal_profile_uses_remote_data_authority_mode(self) -> None:
        profile = config.BOT_PROFILES[config.BotProfileName.PORTAL]

        self.assertIs(config._data_authority_mode(profile), config.DataAuthorityMode.REMOTE)

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

    def test_resolve_data_authority_endpoint_rejects_http_host_in_remote_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "Remote authority endpoints must use https."):
            config.resolve_data_authority_endpoint(
                "http://wakusei.apasz.com",
                None,
                mode=config.DataAuthorityMode.REMOTE,
                public_base_url="https://ignored.example",
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

    def test_resolve_data_authority_endpoint_rejects_http_public_base_fallback_in_remote_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "Remote authority endpoints must use https."):
            config.resolve_data_authority_endpoint(
                None,
                None,
                mode=config.DataAuthorityMode.REMOTE,
                public_base_url="http://wakusei.apasz.com",
                raw_public_base_url="http://wakusei.apasz.com",
            )

    def test_resolve_data_authority_endpoint_allows_http_in_remote_mode_for_dev(self) -> None:
        endpoint = config.resolve_data_authority_endpoint(
            "http://127.0.0.1:8081",
            None,
            mode=config.DataAuthorityMode.REMOTE,
            public_base_url="https://ignored.example",
            allow_insecure_remote=True,
        )

        self.assertEqual(
            endpoint,
            config.AuthorityEndpoint(scheme="http", host="127.0.0.1", port=8081),
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

    def test_resolve_data_authority_server_binding_defaults_to_local_loopback(self) -> None:
        endpoint = config.AuthorityEndpoint(scheme="http", host="wakusei.apasz.com", port=80)

        binding = config.resolve_data_authority_server_binding(None, None, endpoint=endpoint)

        self.assertEqual(binding, config.AuthorityServerBinding(host="127.0.0.1", port=8081))

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


class BotConfigurationTests(unittest.TestCase):
    def test_default_node_capacity_profile_uses_logical_core_count_for_cpu_points(self) -> None:
        with (
            patch("config.psutil.cpu_count", return_value=8) as cpu_count,
            patch("config.psutil.virtual_memory", return_value=Mock(total=4_000 * 1024 * 1024)),
        ):
            profile = config.default_node_capacity_profile(profile=config.BOT_PROFILES[config.BotProfileName.ERIN])

        self.assertEqual(profile.cpu_points_total, 8)
        self.assertEqual(profile.cpu_points_reserved, 2)
        cpu_count.assert_called_once_with(logical=True)

    def test_default_node_capacity_profile_falls_back_to_physical_core_count(self) -> None:
        with (
            patch("config.psutil.cpu_count", side_effect=[None, 4]) as cpu_count,
            patch("config.psutil.virtual_memory", return_value=Mock(total=4_000 * 1024 * 1024)),
        ):
            profile = config.default_node_capacity_profile(profile=config.BOT_PROFILES[config.BotProfileName.ERIN])

        self.assertEqual(profile.cpu_points_total, 4)
        self.assertEqual(profile.cpu_points_reserved, 2)
        self.assertEqual(cpu_count.call_args_list, [call(logical=True), call(logical=False)])

    def test_parse_discord_activity_fields_rejects_duplicates(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not contain duplicate"):
            config.parse_discord_activity_fields("ram, cpu, ram", source="Discord activity field order")

    def test_save_bot_configuration_persists_discord_settings(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "configuration.json"

            config.save_bot_configuration(
                path,
                config.BotConfiguration(
                    discord_settings=config.DiscordSettings(
                        activity=config.DiscordActivitySettings(
                            fallback_text="Watching over Erin",
                            prefix="[",
                            separator=" :: ",
                            suffix="]",
                            refresh_interval_seconds=5,
                            units_per_app=4,
                            alt_text_percentage=25,
                            fields=(
                                config.DiscordActivityField.APP,
                                config.DiscordActivityField.PLAYERS,
                            ),
                        )
                    )
                ),
            )

            loaded = config.load_bot_configuration(path)

        self.assertEqual(loaded.discord_settings.activity.fallback_text, "Watching over Erin")
        self.assertEqual(loaded.discord_settings.activity.prefix, "[")
        self.assertEqual(loaded.discord_settings.activity.separator, " :: ")
        self.assertEqual(loaded.discord_settings.activity.suffix, "]")
        self.assertEqual(loaded.discord_settings.activity.refresh_interval_seconds, 5)
        self.assertEqual(loaded.discord_settings.activity.units_per_app, 4)
        self.assertEqual(loaded.discord_settings.activity.alt_text_percentage, 25)
        self.assertEqual(
            loaded.discord_settings.activity.fields,
            (
                config.DiscordActivityField.APP,
                config.DiscordActivityField.PLAYERS,
            ),
        )

    def test_persisted_oauth_links_omit_unsupported_install_type_when_serialized(self) -> None:
        links = config.PersistedOAuthLinks(guild=None)

        self.assertEqual(links.supported_install_types(), (config.OAuthInstallType.GUILD,))
        self.assertEqual(links.serializable(), {"guild": None})

    def test_load_bot_configuration_reads_capitalised_oauth_key(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "configuration.json"
            path.write_text(
                ('{"OAuth":{"guild":null,"user":"https://discord.com/oauth2/authorize?client_id=123456789012345678"}}'),
                encoding="utf-8",
            )

            loaded = config.load_bot_configuration(path)

        self.assertIsNone(loaded.oauth.guild)
        self.assertEqual(
            loaded.oauth.user,
            "https://discord.com/oauth2/authorize?client_id=123456789012345678",
        )

    def test_save_bot_configuration_preserves_capitalised_oauth_key(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "configuration.json"

            config.save_bot_configuration(
                path,
                config.BotConfiguration(
                    OAuth=config.PersistedOAuthLinks(
                        guild=None,
                        user="https://discord.com/oauth2/authorize?client_id=123456789012345678",
                    )
                ),
            )

            payload = path.read_text(encoding="utf-8")

        self.assertIn('"OAuth"', payload)
        self.assertIn('"guild": null', payload)
        self.assertIn('"user": "https://discord.com/oauth2/authorize?client_id=123456789012345678"', payload)

    def test_save_bot_configuration_omits_unsupported_oauth_key(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "configuration.json"

            config.save_bot_configuration(
                path,
                config.BotConfiguration(
                    OAuth=config.PersistedOAuthLinks(guild=None),
                ),
            )

            payload = path.read_text(encoding="utf-8")

        self.assertIn('"guild": null', payload)
        self.assertNotIn('"user"', payload)

    def test_upsert_known_bot_snapshot_persists_structured_registry(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "configuration.json"
            snapshot = config.BotMetadataSnapshot(
                profile=config.BotMetadataProfile(
                    id="123456789012345678",
                    label="Erin",
                    bot_profile=config.BotProfileName.ERIN,
                ),
                features=config.BotMetadataFeatures(
                    oauth=config.PersistedOAuthLinks(guild=None, user="https://example.com/user")
                ),
            )

            config.upsert_known_bot_snapshot(path, snapshot)
            loaded = config.load_bot_configuration(path)

        self.assertIn("123456789012345678", loaded.known_bots)
        self.assertEqual(loaded.known_bots["123456789012345678"].profile.label, "Erin")
        self.assertEqual(
            loaded.known_bots["123456789012345678"].features.oauth,
            config.PersistedOAuthLinks(guild=None, user="https://example.com/user"),
        )

    def test_load_bot_configuration_reads_maintenance_restart_schedules(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "configuration.json"
            path.write_text(
                '{"maintenance":{"restart_schedules":{"bot":{"enabled":true,"hour":4,"minute":30}}}}',
                encoding="utf-8",
            )

            loaded = config.load_bot_configuration(path)

        schedule = loaded.maintenance.schedule_for(RestartTarget.BOT)
        self.assertTrue(schedule.enabled)
        self.assertEqual(schedule.interval_minutes, 24 * 60)
        self.assertIsNotNone(schedule.anchor_timestamp)
        assert schedule.anchor_timestamp is not None
        anchor_at = datetime.fromtimestamp(schedule.anchor_timestamp).astimezone()
        self.assertEqual((anchor_at.hour, anchor_at.minute), (4, 30))

    def test_restart_schedule_migrates_aware_datetimes_to_unix_seconds(self) -> None:
        schedule = config.PersistedRestartSchedule.model_validate(
            {
                "enabled": True,
                "interval_minutes": 90,
                "anchor_at": "2026-07-01T10:00:00+10:00",
            }
        )

        expected_first_restart = datetime.fromisoformat("2026-07-01T11:30:00+10:00")
        self.assertEqual(schedule.anchor_timestamp, int(expected_first_restart.timestamp()))
        self.assertNotIn("anchor_at", schedule.model_dump(mode="json"))

    def test_load_bot_configuration_backfills_missing_node_capacity(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "configuration.json"
            path.write_text('{"maintenance":{"restart_schedules":{}}}', encoding="utf-8")

            with (
                patch("config.psutil.cpu_count", return_value=8),
                patch("config.psutil.virtual_memory", return_value=Mock(total=4_000 * 1024 * 1024)),
                patch.object(config, "ACTIVE_BOT_PROFILE", config.BOT_PROFILES[config.BotProfileName.YUKI]),
            ):
                loaded = config.load_bot_configuration(path)

            saved_payload = path.read_text(encoding="utf-8")

        self.assertEqual(loaded.node_capacity.cpu_points_total, 8)
        self.assertEqual(loaded.node_capacity.ram_points_total, 8)
        self.assertEqual(loaded.node_capacity.cpu_points_reserved, 3)
        self.assertEqual(loaded.node_capacity.ram_points_reserved, 4)
        self.assertIn('"node_capacity"', saved_payload)
        self.assertIn('"node_font_sources"', saved_payload)

    def test_save_bot_configuration_persists_maintenance_restart_schedules(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "configuration.json"

            config.save_bot_configuration(
                path,
                config.BotConfiguration(
                    maintenance=config.PersistedMaintenanceSettings(
                        restart_schedules={
                            RestartTarget.SYSTEM: config.PersistedRestartSchedule(
                                enabled=True,
                                interval_minutes=135,
                                anchor_timestamp=int(
                                    datetime.fromisoformat("2026-05-27T04:30:00+10:00").timestamp()
                                ),
                            )
                        }
                    )
                ),
            )

            payload = path.read_text(encoding="utf-8")

        self.assertIn('"maintenance"', payload)
        self.assertIn('"restart_schedules"', payload)
        self.assertIn('"system"', payload)
        self.assertIn('"interval_minutes": 135', payload)
        self.assertIn('"anchor_timestamp"', payload)

    def test_save_bot_configuration_persists_steamcmd_path(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "configuration.json"

            config.save_bot_configuration(
                path,
                config.BotConfiguration(steamcmd_path="bash ./steamcmd.sh"),
            )

            loaded = config.load_bot_configuration(path)
            payload = path.read_text(encoding="utf-8")

        self.assertEqual(loaded.steamcmd_path, "bash ./steamcmd.sh")
        self.assertIn('"steamcmd_path": "bash ./steamcmd.sh"', payload)

    def test_steamcmd_command_prefix_uses_bash_for_shell_script_paths(self) -> None:
        self.assertEqual(
            config.steamcmd_command_prefix("./steamcmd.sh"),
            ("bash", "./steamcmd.sh"),
        )

    def test_steamcmd_command_prefix_keeps_binary_name_as_direct_command(self) -> None:
        self.assertEqual(
            config.steamcmd_command_prefix("steamcmd"),
            ("steamcmd",),
        )

    def test_build_local_bot_metadata_snapshot_uses_profile_and_oauth(self) -> None:
        snapshot = config.build_local_bot_metadata_snapshot(
            bot_id="123456789012345678",
            label="Yuki",
            bot_profile=config.BotProfileName.YUKI,
            oauth=config.PersistedOAuthLinks(guild="https://example.com/guild", user=None),
            mod_web=config.BotMetadataModWeb(
                node_name="yuki",
                public_base_url="http://yuki.example:3180",
                node_api_base_url="http://yuki.example:3180/api/node",
            ),
            presentation=config.BotMetadataPresentation(
                avatar_uri=" https://cdn.example.com/yuki.png?size=128 ",
                accent_color_hex=" #7C3AED ",
            ),
        )

        self.assertEqual(snapshot.profile.id, "123456789012345678")
        self.assertEqual(snapshot.profile.label, "Yuki")
        self.assertIs(snapshot.profile.bot_profile, config.BotProfileName.YUKI)
        self.assertEqual(snapshot.features.oauth, config.PersistedOAuthLinks(guild="https://example.com/guild"))
        self.assertEqual(snapshot.features.mod_web.node_name if snapshot.features.mod_web is not None else None, "yuki")
        self.assertEqual(
            snapshot.features.presentation,
            config.BotMetadataPresentation(
                avatar_uri="https://cdn.example.com/yuki.png?size=128",
                accent_color_hex="#7c3aed",
            ),
        )

    def test_sync_remote_bot_metadata_posts_structured_snapshot(self) -> None:
        snapshot = config.build_local_bot_metadata_snapshot(
            bot_id="123456789012345678",
            label="Erin",
            bot_profile=config.BotProfileName.ERIN,
            oauth=config.PersistedOAuthLinks(guild=None, user=None),
        )
        client = Mock()
        client.post_json.return_value = {"data": snapshot.model_dump(mode="json")}

        with patch.object(config, "authority_client", return_value=client):
            synced = config.sync_remote_bot_metadata(snapshot)

        client.post_json.assert_called_once_with(
            "/authority/bots/sync",
            {"data": snapshot.model_dump(mode="json")},
        )
        self.assertEqual(synced, snapshot)

    def test_supported_oauth_install_types_parses_application_config(self) -> None:
        application = Mock()
        application.integration_types_config = {"0": {}, 1: {}}

        supported = config.supported_oauth_install_types(application)

        self.assertEqual(
            supported,
            frozenset({config.OAuthInstallType.GUILD, config.OAuthInstallType.USER}),
        )

    def test_supported_oauth_install_types_parses_hikari_enum_keys(self) -> None:
        application = Mock()
        application.integration_types_config = {
            applications.ApplicationIntegrationType.GUILD_INSTALL: {},
            applications.ApplicationIntegrationType.USER_INSTALL: {},
        }

        supported = config.supported_oauth_install_types(application)

        self.assertEqual(
            supported,
            frozenset({config.OAuthInstallType.GUILD, config.OAuthInstallType.USER}),
        )

    def test_sync_local_oauth_configuration_removes_unsupported_install_type(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "configuration.json"
            config.save_bot_configuration(
                path,
                config.BotConfiguration(
                    OAuth=config.PersistedOAuthLinks(guild=None, user="https://example.com/user"),
                ),
            )

            synced = config.sync_local_oauth_configuration(
                path,
                supported_install_types=(config.OAuthInstallType.GUILD,),
            )

        self.assertEqual(synced.oauth.supported_install_types(), (config.OAuthInstallType.GUILD,))
        self.assertIsNone(synced.oauth.guild)
        self.assertFalse(synced.oauth.supports(config.OAuthInstallType.USER))


if __name__ == "__main__":
    unittest.main()
